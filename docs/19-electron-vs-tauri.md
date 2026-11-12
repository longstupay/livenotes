# 19 · Electron vs Tauri 2：事实核查、逐项对比与「多功能工作台」架构

> **结论先行：A2 选 Electron。**
> **2026-11-10 决策更新：项目最终选择 Tauri 2，见 [docs/20](20-tauri-2-adr.md)。
> 本文的对比数据保留；第 7 节的建议被有意推翻，不是遗忘。**
> 不是因为 Electron 更流行，而是因为我们的核心能力——「内嵌第三方页面 + 单视图采集 +
> 多工具隔离」——正好落在 Electron 最成熟、Tauri 2 最需要自己补的那块。
> 体积和内存的差距，在「Python 引擎 + 257MB 模型 + ffmpeg」的总量面前只有约 25%，
> 不值得用采集稳定性和迭代速度去换。
>
> 但壳不是资产：**引擎（Python）和工具契约才是**。下面第 5 节给出一套
> 「壳可替换、工具可叠加」的架构，保证今天选 Electron、以后想换 Tauri 也不用重写业务。

---

## 1. 事实核查：Codex、Claude、VS Code 到底是什么壳

这不是猜的。直接看本机安装目录（Windows 10，2026-11）：

| 应用 | 证据 | 结论 |
|---|---|---|
| **Claude Desktop** | 安装目录有 `LICENSES.chromium.html`、`chrome_100_percent.pak`、`ffmpeg.dll`、`icudtl.dat`、`resources/app.asar`；`app.asar` 里能搜到 **16 处 `WebContentsView`**、2 处 `desktopCapturer` | **Electron**（MSIX 打包） |
| **Codex 桌面端** | `OpenAI.Codex_26.1103.3517.0_x64` 里有 `ChatGPT.exe` / `Codex.exe`、`chrome.dll`、`resources.pak`、`v8_context_snapshot.bin`；`resources/app.asar` **726MB**；`owl-electron-app.json` 写明 `packagedFrom ... electron/out/Codex-win32-x64`；asar 里能搜到 `WebContentsView`、`BrowserView`、`loadExtension`；Chromium 157 | **Electron 壳 + Rust sidecar**（`codex.exe`、`codex-windows-sandbox-service.exe` 等） |
| **VS Code** | 公开事实：Electron + extension host + language server | **Electron** |
| **Cursor** | VS Code 分支 | **Electron** |

两个细节很说明问题：

1. **Codex 的壳是 Electron，引擎是 Rust。** OpenAI 不缺 Rust 工程师，但仍然把
   「窗口、标签、浏览器面板、扩展加载」交给 Electron，把「agent、沙箱、执行器」做成
   `codex.exe` 这样的原生 sidecar。这正是我们「Electron 壳 + Python 引擎」的同构做法。
2. **Claude Desktop 的右侧面板大概率就是 `WebContentsView`。** asar 里有 16 处该 API，
   配合它的分栏形态基本可以确定：文件树、浏览器、终端不是一个系统窗口，而是同一个
   Electron 窗口里的多个 Chromium 子视图。
3. **Codex 的 `app.asar` 有 726MB。** 它并没有因为「包大」就放弃 Electron；
   对工作台类应用，包体积早就不是第一约束。

> 结论：**「Electron 壳 + 原生引擎 sidecar」是 AI/开发工作台的主流形态**，
> 不是技术债，也不是小团队妥协。

---

## 2. 它们为什么这么选

| 原因 | 具体是什么 | A2 是否同样成立 |
|---|---|---|
| 内嵌浏览器 | 工作台需要把第三方页面（文档、控制台、预览、登录页）装进自己的窗口；Electron 的 `WebContentsView` 是现成原语 | **完全成立**：直播间、生图工具、中转站后台都要内嵌 |
| 一套 Chromium | Win/mac/Linux 行为一致，DevTools/CDP/扩展/媒体能力可复现 | 成立；采集最怕内核行为漂移 |
| Node 生态 | 托盘、更新、签名、密钥、通知、代理、崩溃报告都有成熟库 | 成立；壳层脏活多，生态省周 |
| 原生能力下沉 | Rust/C++/Python 做成 sidecar，通过本地 API 通信，不跟 UI 混在一起 | 成立；我们已经有 Python 引擎 |
| 迭代速度 | Web 技术栈 + 热更新 + AI 辅助编码 | 成立；1 人团队最缺的是时间 |

Tauri 的卖点是「小、快、安全默认」。这三个都对，但它们解决的是**另一个产品**的问题：
一个 3~5 个页面、不内嵌第三方内容、不需要浏览器自动化的本地工具。

---

## 3. Electron vs Tauri 2 逐项对比

### 3.1 渲染内核与版本

| | Electron | Tauri 2 |
|---|---|---|
| Windows | 自带 Chromium，版本随 Electron 锁定 | 系统 WebView2（Edge，常青，随 Windows 更新） |
| macOS | 同一套 Chromium | WKWebView |
| Linux | 同一套 Chromium | WebKitGTK |
| 行为可复现 | **高**：装的是哪版就是哪版 | **低**：用户机器上的 Edge/WebKit 版本不同 |
| 安全补丁 | 需要自己跟 Electron 版本发更新 | 随系统更新，维护轻 |
| 内核能力 | 固定、可测 | 随平台/版本变化，需要矩阵测试 |

对采集类功能，「行为可复现」比「自动打补丁」更重要：我们现在就被
Chrome 137 忽略 `--load-extension`、Edge 非正常退出禁用扩展坑过两次。
Electron 至少把「内核」变成了我们可以锁定和回归的依赖。

### 3.2 内嵌浏览器与多视图

| | Electron | Tauri 2 |
|---|---|---|
| 原语 | `WebContentsView`（Electron 30+，官方推荐） | `WebviewBuilder` / `WebviewWindow`，窗口内子 webview |
| 布局 | `setBounds`，和主 UI 同窗口合成 | 子 webview 定位/缩放/焦点要手动管理，平台差异明显 |
| 登录态隔离 | `session.fromPartition('persist:xxx')`，一行一个分区 | `data_directory` / incognito，粒度较粗 |
| 静音单视图 | `webContents.setAudioMuted(true)` | 需要 `with_webview` 拿 `ICoreWebView2Controller`，调 COM `put_IsMuted` |
| CDP / 自动化 | `webContents.debugger.attach()` 原生 | `CallDevToolsProtocolMethod`，经 `with_webview` + COM |
| 权限拦截 | `session.setPermissionRequestHandler` | WebView2 `PermissionRequested` 事件，经 COM |
| Chrome 扩展 | `session.loadExtension`（部分支持，Chrome 专有 API 不一定有） | 无官方支持 |

这张表是选型的核心。我们要做的不是「显示一个网页」，而是**对每个网页做细粒度控制**：
静音、注入、CDP、cookie 分区、权限拒绝、崩溃回收。Electron 把这些做成了公开 API；
Tauri 2 能做到，但每一条都要落到 Rust + 平台 COM，Windows 一套、mac 另一套。

### 3.3 采集与自动化

| 能力 | Electron | Tauri 2 |
|---|---|---|
| 注入 `video.captureStream()` | `executeJavaScript` / preload，直接可用 | `eval` / initialization script，可用 |
| 16k PCM → 本地服务 | IPC 或直接 POST，Node 侧转发 | Rust command 或前端 fetch |
| 单视图静音不影响采集 | `setAudioMuted`，需 spike 验证 | COM `put_IsMuted`，需 spike 验证 |
| 后台/最小化不断播 | `backgroundThrottling:false` + `--disable-renderer-backgrounding` | WebView2 参数 + `IsVisible` 行为，需逐项验 |
| 自动播放 | `autoplayPolicy` / 启动参数 | WebView2 参数 |
| 多路并发 | 每个 view 一个 renderer，Electron 主进程统一管 | 每个 webview 一个 WebView2，Rust 侧管 |
| 崩溃恢复 | `render-process-gone` 事件，重建 view | `ProcessFailed` 事件，重建 webview |

结论：两条路都能采，但 Electron 的每一步都有现成 API 和大量先例；Tauri 的每一步
都要先证明「WebView2 这版行为是这样」，而 WebView2 会随 Edge 更新变化。

### 3.4 语言、团队与迭代速度

| | Electron | Tauri 2 |
|---|---|---|
| 壳层语言 | TypeScript / JavaScript（Node） | Rust |
| UI 语言 | 任意 Web（我们现有 ESM 静态页） | 任意 Web（同一套） |
| 引擎 | Python sidecar（不变） | Python sidecar（不变） |
| 语言总数 | **2**（TS + Python） | **3**（Rust + TS + Python） |
| 生态 | npm 海量桌面/系统库 | crate 生态强，但桌面集成插件少 |
| AI 辅助编码 | 训练语料极多，生成质量高 | Rust/COM 边缘案例多，AI 容易给出似是而非的代码 |
| 招人/交接 | 容易 | 难 |

对 1 人 + AI 的团队，「少一门语言、少一层 COM」不是小事。工作台的功能越多，
壳层要补的原生能力越多，Rust 的边际成本越高。

### 3.5 体积与内存：真实差距没有想象中大

| 组成 | Electron 方案 | Tauri 2 方案 |
|---|---|---|
| Shell | ~180–250MB（Electron） | ~10–20MB |
| Python 运行时 + 依赖 | ~250–350MB | 同 |
| 模型 | 257MB | 同 |
| ffmpeg | ~80MB | 同 |
| **安装后合计** | **~750–900MB** | **~550–700MB** |
| 安装包（压缩） | ~350–450MB | ~280–380MB |
| 空载内存 | 200–300MB | 80–150MB |
| 3 路内嵌采集时 | 2.5–3.5GB | 2.3–3.3GB |

差距约 **150–200MB 磁盘、100–150MB 内存**。而采集时真正的内存大头是每个直播
renderer（共享模式实测每标签页边际 ~0.56GB）和 ASR 引擎（~0.5GB）。为了省这
100MB，去承担采集行为不可复现的风险，不划算。

> 参考：Codex 的 Electron `app.asar` 就有 726MB；Claude 的 asar 77MB。
> 工作台类应用的用户，对 700MB 安装包并不敏感，对「能不能用」很敏感。

### 3.6 安全模型

| | Electron | Tauri 2 |
|---|---|---|
| 默认安全 | 需要手动加固（`sandbox` / `contextIsolation` / 禁 `nodeIntegration`） | capability 模型，默认权限小 |
| 多页面隔离 | `session` 分区 + 权限 handler + `webRequest` 白名单，粒度细 | 每个 webview 的能力配置，粒度也够，但生态经验少 |
| 加载第三方页面 | 成熟套路：独立分区、拒绝权限、拦截导航/弹窗 | 同样要做，但 WebView2 的权限/导航控制要经 COM |
| 密钥存储 | `safeStorage`（Windows DPAPI） | `keyring` / stronghold 插件 |
| 代码完整性 | ASAR integrity + fuses + 签名 | 签名 + 更新签名 |
| 攻击面 | 大（Node + Chromium） | 小（无 Node） |

Tauri 的默认值更好；但「工作台要安全地装很多第三方页面」这件事，Electron 的
`session` / `permission` / `webRequest` 三件套更成熟、资料更多。两者都必须做安全清单，
Electron 的清单更长，但每一步都有先例。

### 3.7 跨平台一致性

| | Electron | Tauri 2 |
|---|---|---|
| Windows | Chromium 锁定 | WebView2 常青 |
| macOS | 同一套 Chromium | WKWebView（媒体/采集行为差异大） |
| Linux | 同一套 Chromium | WebKitGTK（媒体栈最不稳定） |
| 采集回归 | 一次验证，三平台同构 | 三套内核，三套验证 |
| 未来 mac 支持 | 成本低 | 成本高，采集可能重做 |

我们现在虽然只做 Windows（`pycaw` 进程静音也是 Windows 专属），但架构不该把
「以后上 mac」的路堵死。Electron 是更稳的跨平台底座。

### 3.8 更新、分发与调试

| | Electron | Tauri 2 |
|---|---|---|
| 自动更新 | `electron-updater`，差分更新成熟 | `tauri-plugin-updater`，够用 |
| 安装器 | NSIS / MSI / MSIX（Claude、Codex 都用 MSIX） | NSIS / MSI |
| 代码签名 | 成熟 | 成熟 |
| 国内更新源 | OSS/R2 + 差分 | OSS/R2 |
| 崩溃报告 | Sentry 等成熟 | 可用，生态小 |
| 调试 | DevTools + CDP + `webContents.debugger` | DevTools，但进程/COM 层调试更费力 |
| 构建链 | Node + electron-builder | Rust + tauri-cli |

### 3.9 移动端

Tauri 2 支持 iOS/Android，Electron 不支持。但 A2 的移动端需求是「手机看回放」，
现在 `--lan` + 浏览器已经解决；真要做移动 App，也是独立客户端（Flutter/RN），
不会用桌面壳的移动构建。**这一项对 A2 不加分。**

---

## 4. 一张表总结 + A2 加权评分

| 维度 | 权重 | Electron | Tauri 2 | 说明 |
|---|---:|---:|---:|---|
| 内嵌浏览器与采集可控性 | 30% | 5 | 2 | 核心能力，差距最大 |
| 功能迭代速度 / 生态 | 25% | 5 | 2 | 1 人 + AI，速度就是生命 |
| 体积 / 内存 | 15% | 3 | 4 | 差距 150–200MB，非决定性 |
| 跨平台一致性 | 15% | 5 | 2 | 采集行为可复现 |
| 安全默认值 | 10% | 3 | 4 | Tauri 略优，但都要做清单 |
| 壳可替换性 | 5% | 4 | 4 | 取决于架构，不取决于壳 |
| **加权** | | **4.45** | **2.60** | |

> 如果产品是「3 个页面 + 本地 Python 服务 + 不内嵌第三方内容」，把权重换成
> 「体积 40% / 速度 20% / 内嵌 10%」，Tauri 会赢。**选型要跟着产品形态走，不是跟着喜好走。**

---

## 5. 「以后加很多功能」应该怎么架构

壳选 Electron 只解决了「窗口和浏览器视图」。真正决定能不能加 20 个功能的，是下面这套分层。

### 5.1 三层 + 一条硬边界

```
┌─ Shell（Electron，薄）────────────────────────────────────┐
│  主窗口 / 标签条 / 每个工具一个 WebContentsView           │
│  托盘、通知、全局快捷键、深链、单实例                     │
│  safeStorage（密钥）、自动更新、崩溃报告                 │
│  本地 API 网关（可选）：统一注入 token、代理、限流        │
└───────────────────────┬───────────────────────────────────┘
                        │ 只走 HTTP/WS/SSE（127.0.0.1 + token）
┌───────────────────────▼───────────────────────────────────┐
│  Engine（Python，厚）                                     │
│  采集 / ASR / 声纹 / 总结 / SQLite / 任务队列 / 调度      │
│  Provider 适配层：LLM、生图、中转站、重试/配额/计费       │
│  工具 API：/api/tools/<tool-id>/...（按 scope 鉴权）      │
└───────────────────────┬───────────────────────────────────┘
                        │
              ┌─────────▼─────────┐
              │  Tool packages    │
              │  manifest + 静态页 │
              │  live / image /   │
              │  relay / ...      │
              └───────────────────┘
```

**硬边界：工具页面永远不直接调 Electron API，只调 Engine HTTP。**
这样工具可以原样跑在浏览器/PWA 里，未来换壳也不用重写。

### 5.2 六条契约

1. **Engine 是唯一数据源。** 会话、录音、设置、密钥、任务都在 Engine；Shell 只存窗口布局。
2. **工具只认 HTTP。** 每个工具一个 API 命名空间；工具之间不能互相调用。
3. **每个工具一个 manifest。** 声明 id、入口、图标、权限、最低 Engine 版本。
4. **每个工具一个隔离分区。** `persist:tool-<id>`；cookie、storage、缓存互不可见。
5. **生命周期可控。** 打开才挂载，关闭即销毁；长任务在 Engine，不靠页面保活。
6. **版本可协商。** Engine API 带版本；Shell/工具声明兼容范围；升级不互相绑架。

manifest 示例：

```json
{
  "id": "image-gen",
  "title": "AI 生图",
  "icon": "image.svg",
  "entry": "/tools/image-gen/index.html",
  "api": "/api/tools/image-gen",
  "permissions": {
    "network": ["api.openai.com", "relay.example.com"],
    "files": ["outputs/images"],
    "session": "persist:tool-image-gen"
  },
  "minEngine": "1.4"
}
```

### 5.3 工具怎么加

| 阶段 | 做法 | 成本 |
|---|---|---|
| 第一阶段（现在） | 工具就是 `app/web/tools/<id>/` 下的静态页 + Engine 里一个 API 模块 | 一个新工具 = 1 个目录 + 1 个路由文件 |
| 第二阶段 | 工具包独立构建（可用任意框架），产物是静态目录；Shell 按 manifest 挂载 | 构建链在工具自己目录，不污染主仓库 |
| 第三阶段 | 签名工具包 + 权限审批；第三方工具进独立分区 | 需要签名/审核/沙箱规范 |
| 第四阶段 | 外部连接器走 MCP：Engine 作为 MCP client，把外部工具暴露给 LLM | 与 AI 工作台天然契合 |

**不要一上来做插件市场。** 先把 3~5 个第一方工具按 manifest 跑通，验证权限和生命周期，
再考虑第三方。

### 5.4 中转站 / 生图这类功能放哪

| 关注点 | 放在 Engine 的理由 |
|---|---|
| API Key | 只在 Engine 内存/DPAPI 加密文件里；工具页面永远拿不到 |
| CORS | 本地 HTTP 调用没有浏览器跨域问题 |
| 重试/降级/配额 | 一个适配层统一做，工具不用重复实现 |
| 计费/日志 | 每次调用落 SQLite，按工具/模型/用户统计 |
| 流式输出 | Engine 透传 SSE，Shell/工具只负责渲染 |
| 多 Provider | 现有 `app/settings.py` 已经是 OpenAI 兼容的 base/key/model 多配置，扩展成适配器即可 |

Shell 只做两件事：设置界面（填 base/key/model）+ 用量面板。**不要把 provider 调用写进 Electron 主进程**，
否则以后换壳/上服务器/做团队共享都要重写。

### 5.5 内存与并发策略（16GB 机器的现实约束）

- 每个工具视图按需挂载，关闭即 `webContents.close()`；
- 同时最多 2~3 个内嵌直播视图，其余走 `direct`；
- 工具页不用时 `setBackgroundThrottling(true)`，直播视图才 `false`；
- Engine 的 ASR 池已有并发控制，继续沿用；
- 托盘显示「当前 N 路采集 / 内存占用」，超过阈值提示切 direct。

这套策略在 Electron 和 Tauri 上都成立，但 Electron 的视图生命周期 API 更直接。

---

## 6. 如果最终选 Tauri：先证明这 6 件事

不要先写业务，先写一个 3~5 天的 spike，逐条打勾：

1. WebView2 子视图里注入 `video.captureStream()`，稳定出 16k PCM；
2. `put_IsMuted(true)` 后 `captureStream()` RMS 不变；
3. 视图隐藏/窗口最小化 10 分钟，直播不断流；
4. 每个 webview 独立 `data_directory`，登录态互不串；
5. `CallDevToolsProtocolMethod` 能替代现有 CDP 控制面（启动探针、页面探针）；
6. 3 路并发 1 小时，内存/CPU 可接受，崩溃能自动重建。

任何一条不过，Tauri 的方案就要重新估。Electron 同样要做 1~4，但 API 路径短得多。

---

## 7. 最终建议

1. **壳选 Electron**，按 docs/18 的 C1 → C2 分两期：先壳，后内嵌浏览器。
2. **引擎保持 Python**，继续用 `run.py` 作为开发主路径；Electron 只是另一个消费者。
3. **从第一天就按第 5 节的三层契约写**：Shell 薄、Engine 厚、工具 manifest 化。
   这样即使两年后想换 Tauri，只需要重写壳层，Engine 和工具原样保留。
4. **采集仍然 direct 优先**；内嵌浏览器只做兜底，最多 2~3 路。
5. **先做 spike 再全量**：Electron 验 4 件事（captureStream、静音、后台、内存），
   2~3 天就能给出答案。

一句话：**Electron 是今天更快的选择，而第 5 节的架构让这个选择不至于变成明天的枷锁。**
