# 18 · 打包成桌面应用：五种外壳方案，以及「内置浏览器」怎么落地

> **结论先行：能打包，而且现在这个形态（Python 本地服务 + 静态页面）恰好是最容易套壳的形态。**
> 推荐 **方案 C：Electron 壳，分两期**——第一期只做壳（采集仍走现在的浏览器），
> 第二期把直播间做成应用内的浏览器视图。采集底座仍然 **direct 优先**，内嵌浏览器只做兜底。
> 如果坚持「仓库里不出现 Node 工具链」，退到 **方案 B（pywebview）**，但要接受
> 「没有应用内多标签浏览器」，未来做工作台时大概率还要再迁一次。
>
> 本文只做决策，不改代码。所有工作量按「1 人 + AI 辅助、以当前 17.7k 行 Python +
> 10 个静态页为基准」粗估。
> Electron 与 Tauri 2 的逐项深挖（含本机实测 Codex / Claude Desktop 的壳类型）见
> [docs/19](19-electron-vs-tauri.md)。
> **2026-11-10 决策更新：最终选择 Tauri 2，见 [docs/20](20-tauri-2-adr.md)。**
> 本文的五种方案与打包清单保留，作为决策依据。

---

## 1. 先回答：Codex / VS Code 的「内置浏览器」是怎么做到的

不是黑魔法，也不是去控制你系统里的 Chrome。**它们自己带了一个 Chromium，
在窗口里开一个子视图来渲染网页。**

### 1.1 三类实现

| 做法 | 谁在用 | 内核从哪来 | 代价 |
|---|---|---|---|
| **Electron / CEF** | VS Code、Cursor、Slack、Discord、Notion、Spotify（CEF） | 安装包里自带一份 Chromium | 安装包 +150~250MB；每个页面一个 renderer 进程 |
| **WebView2 / WKWebView** | Tauri、Wails、pywebview、很多 Windows 小工具 | 用系统里的 Edge / Safari 内核 | 安装包小；但内核版本随系统变，行为不可完全控 |
| **外部浏览器 app 模式** | 很多「伪桌面端」 | 用户自己的 Chrome/Edge | 最省事；但窗口、进程、标签页都不归你管 |

VS Code / Cursor 是 Electron；Codex 桌面端右侧那个网页面板，从形态看也是同一路数：
**主窗口是应用自己的 UI，旁边的网页是嵌进来的 Chromium 视图**，不是另开了一个系统浏览器。

### 1.2 在 Electron 里，「一个标签页」具体是什么

```
┌─ BaseWindow ─────────────────────────────────────────────┐
│  应用自己的 UI（BrowserWindow / WebContentsView）          │
│  ┌─ 标签条（HTML 画的，想长什么样就长什么样）───────────┐  │
│  │ [直播间 A] [直播间 B] [工具·生图] [工具·中转站]      │  │
│  └──────────────────────────────────────────────────────┘  │
│  ┌─ WebContentsView（每个标签一个）─────────────────────┐  │
│  │  https://live.douyin.com/xxx                        │  │
│  │  · 独立 renderer 进程                               │  │
│  │  · 独立 cookie / localStorage（session partition）   │  │
│  │  · 可以 setAudioMuted / 调 DevTools / 注入脚本       │  │
│  └──────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────┘
```

关键 API（Electron 30+）：

- `WebContentsView`：一个网页视图，可 `setBounds` 嵌进窗口，替代已废弃的 `BrowserView`。
- `webContents.session`：cookie / 缓存 / 登录态。用 `persist:live` 分区，**一次登录所有房间共用**。
- `webContents.setAudioMuted(true)`：只掐扬声器，不影响页面内部的 `captureStream()`（和现在扩展里
  `chrome.tabs.update({muted:true})` 是同一层，需 spike 验证）。
- `webContents.executeJavaScript()` / preload：往页面里注入采集脚本。
- `session.setDisplayMediaRequestHandler()`：可以自动应答屏幕共享，但它的 `audio:'loopback'`
  是**系统混音**，不是「单个标签页音频」——多路房间不能用它，只能走注入。

### 1.3 所以「开浏览器标签页」这个功能不会消失，只是要分三类处理

| 链接类型 | 打包后怎么开 |
|---|---|
| 我们自己的页面（首页、rooms、reader…） | 应用主窗口加载 `http://127.0.0.1:<port>/`，页面代码一行不改 |
| 直播间（要采集） | 应用内 `WebContentsView`（推荐）；或继续用专用外部浏览器（兜底） |
| 登录 / 扫码 / 支付 / 平台验证 | `shell.openExternal()` 交给系统浏览器，办完回来 |

**不是二选一，是三种链接三种打开方式。** 很多桌面应用都保留「在系统浏览器中打开」这个逃生口。

---

## 2. 我们现在的「开浏览器」有三处，别混在一起

| 位置 | 现在做什么 | 打包后 |
|---|---|---|
| `run.py:99` | `webbrowser.open(127.0.0.1:8765)` 打开自己的 UI | 壳的主窗口；设 `LIVENOTES_NO_BROWSER=1` 关掉自动开浏览器 |
| `tools/capture_agent.py` | `per_room`：每个房间拉起一个 Chrome，打开 `/capture`，靠 `--auto-select-desktop-capture-source` 免选择 | 内嵌视图注入采集；或保留外部专用浏览器；`direct` 模式本来就不需要浏览器 |
| `extensions/livehub` | `shared`：一个 Edge 开 N 个标签页，扩展注入 `captureStream()` | 内嵌视图后**不再需要扩展**，但注入逻辑可以原样搬过来 |

真正需要重新设计的只有第二处。而且——

### 2.1 内嵌浏览器对采集是利好，不是障碍

现在为了「不打扰用户地采一路标签页音频」，我们绕了很多路：

- Chrome 137 起忽略 `--load-extension`，shared 模式只能退回 Edge / Chrome for Testing；
- `tabCapture.getMediaStreamId` 对后台标签页要用户手势，只能改用 `captureStream()`；
- 每个房间一个独立 `--user-data-dir`（`data/profiles/rm*`），**每个房间都要重新登录**；
- Edge 非正常退出会把未打包扩展标成 disabled，代理还得去清 Secure Preferences；
- `--auto-select-desktop-capture-source` 是 Chromium 的非公开参数，版本一变就可能失效。

内嵌视图把这些全部变成我们自己进程里的事：

1. **一个登录态所有房间共用**（`persist:live` 分区），不用再为每个房间扫码；
2. **不需要扩展**，`executeJavaScript` 直接注入，绕开 Chrome 137 / Secure Preferences 两座山；
3. **不需要非公开启动参数**，`getDisplayMedia` 的选择器问题不再存在；
4. 关房间就是 `view.webContents.close()`，不会残留 Chrome 进程和 profile；
5. 页面加载失败、崩溃、白屏，主进程能直接拿到事件，状态比 CDP 轮询更准。

但有三个必须先用 spike 验证的点：

- **静音不影响采集**：`setAudioMuted(true)` 之后 `captureStream()` 的 RMS 是否仍然正常；
- **后台不掉播放**：`backgroundThrottling:false` + `--disable-renderer-backgrounding` 下，
  抖音在视图隐藏/窗口最小化时是否仍持续出流；
- **单视图内存**：共享模式实测每标签页边际约 0.56GB；Electron 视图大概率同一量级，
  16GB 机器上不能按「5 路都内嵌」来设计。

### 2.2 采集策略不变：direct 优先，浏览器兜底

`direct` 模式 5 路实测 139MB，是这台机器上唯一能放心开 5 路的底座。打包后应保持：

```
抖音链接 → direct（ffmpeg，无浏览器）
  失败/非抖音/需要登录 → 内嵌浏览器视图（最多 2~3 路）
  内嵌也失败 → 「在系统浏览器打开」交给外部专用浏览器
```

**内嵌浏览器不是用来替代 direct 的，是用来替代「外部 Chrome + 扩展」这条兜底链路的。**

---

## 3. 打包之前必须先收口的六件事

这六件与选哪个壳无关，先做哪条路都要做：

| # | 事项 | 现状 | 要做 |
|---|---|---|---|
| 1 | Python 运行时 | 用户自己装 Python + pip | PyInstaller **onedir**（不要 onefile：250MB 模型每次解压，启动慢且杀软敏感）；sherpa-onnx / onnxruntime 的原生 DLL 要 collect |
| 2 | 数据目录 | `app/config.py` 默认 `ROOT/data`（当前 5.4GB） | 壳启动时设 `LIVENOTES_DATA_DIR=%LOCALAPPDATA%\LiveNotes\data`；首次启动可导入旧 `data/` |
| 3 | 模型 | `models/` 257MB，`tools/fetch_models.py` 下载 | 随安装包分发（本地优先）或首启下载；许可要逐项确认（尤其 SenseVoice / 声纹模型是否允许随商业包分发） |
| 4 | ffmpeg | 用户自己装并进 PATH | 随包带一份静态构建；注意 GPL/LGPL 再分发义务，优先 LGPL build |
| 5 | 三个进程 | `start.bat` 开三个 cmd 窗口：server / capture_agent / feishu_bot | 壳统一拉起、守护、退出时全杀；端口启动时选空闲口（`LIVENOTES_PORT` 已支持），`--base` 传给代理；不再有黑窗口 |
| 6 | 签名 / 更新 | 无 | Windows 代码签名（OV/EV）；`electron-updater` 或自建更新；国内更新源不要放 GitHub 直连 |

另外两个产品化细节：

- **单实例**：命名互斥体/锁文件，第二次启动只聚焦已有窗口。
- **手机访问**：只有用户显式打开「局域网访问」才 `--lan`；安装包顺带加防火墙规则，
  否则第一次弹 Windows 防火墙会吓到用户。

---

## 4. 五种方案

### 方案 A · 轻壳：安装包 + 系统浏览器 app 模式

**形态**：PyInstaller 打出 `LiveNotes.exe`，双击后启动本地服务，再用
`msedge --app=http://127.0.0.1:<port>/` 打开一个无地址栏窗口。托盘常驻，退出时杀子进程。

```
LiveNotes.exe（PyInstaller onedir）
  ├─ engine.exe        本地服务 + 采集代理 + 飞书 bot（同进程或子进程）
  └─ 调 shell.open → msedge --app=...  ← 窗口是浏览器的
```

| 项 | 值 |
|---|---|
| 内置浏览器 | **没有**。窗口是系统 Edge/Chrome 的 app 模式 |
| 直播间 | 和现在完全一样：外部专用 Chrome，三套采集底座原样保留 |
| 安装体积 | ~450–600MB（Python + 模型 + ffmpeg） |
| 工作量 | **3–5 人天** |
| 优点 | 风险最低；采集链路一行不改；`python run.py` 开发方式保留；先把「安装包、数据目录、托盘、日志、单实例」这些脏活干完 |
| 缺点 | 不像一个真正的应用：窗口归浏览器、没有应用内标签、没有原生菜单；用户关掉窗口后服务还在托盘里，容易被误以为没退出 |
| 什么时候选 | 只想先摆脱「命令行 + 三个黑窗口」；或者作为 C 的 **P0** |

> 补充：Edge 的「安装此站点为应用」今天就能用，零开发。可以作为方案 A 之前的临时体验。

### 方案 B · 原生窗口：pywebview / WebView2

**形态**：Python 单进程，`webview.create_window()` 起一个原生窗口，内部是系统 WebView2；
服务跑在后台线程。托盘用 pystray，安装包用 PyInstaller + Inno Setup。

| 项 | 值 |
|---|---|
| 内置浏览器 | 只有**主窗口**这一个 WebView2；没有多标签、没有第三方页面容器 |
| 直播间 | 仍走外部浏览器；若要内嵌，得自己开多个 WebView2 窗口并 `ExecuteScriptAsync` 注入，工作量陡增且行为要重新验 |
| 安装体积 | ~400–550MB（不含 Chromium） |
| 工作量 | **6–10 人天** |
| 优点 | 仓库不引入 Node；启动快；内存比 Electron 低；Python 团队零学习成本 |
| 缺点 | WebView2 的能力边界就是你的产品边界：多视图、DevTools、扩展、下载、权限管理都要自己补；未来做「AI 工作台」时，内嵌第三方工具页会很别扭 |
| 什么时候选 | 明确只要一个「本地工具」，不打算做多标签工作台；或者对安装体积/内存极其敏感 |

### 方案 C · Electron 壳 + 内嵌浏览器（推荐，分两期）

**形态**：

```
Electron 主进程
  ├─ 主窗口：加载 http://127.0.0.1:<port>/（现有 10 个页面，零改动）
  ├─ WebContentsView × N：直播间 / 未来的工具页
  ├─ 托盘、菜单、全局快捷键、通知、单实例、自动更新
  └─ 子进程：engine.exe（PyInstaller，server + capture_agent + feishu_bot）
```

**C1（只做壳，采集不变）**：Electron 只负责窗口、托盘、进程守护、更新；直播间仍由
capture_agent 拉起外部 Chrome。**1.5–2 周**。这一步就能交付「像个应用」的版本。

**C2（内嵌浏览器）**：每个房间一个 `WebContentsView`，preload/`executeJavaScript` 注入
`video.captureStream()` → 16k PCM → IPC → 主进程转发 `/api/live/feed`；`setAudioMuted(true)`；
共享 `persist:live` 登录态；标签条用 HTML 画。**再加 2–3 周**。

| 项 | 值 |
|---|---|
| 内置浏览器 | **真正的 Chromium 多视图**，和 Codex/VS Code 同一路数 |
| 直播间 | direct 优先；内嵌视图兜底；保留「用系统浏览器打开」 |
| 安装体积 | ~650–800MB（Electron ~180MB + Python 运行时/依赖 ~250MB + 模型 257MB + ffmpeg） |
| 工作量 | C1 10–15 人天；C2 再 10–15 人天；合计 **4–6 周** |
| 优点 | 现有 UI 零改动（还是那套 ESM 静态页，docs/11 的「无构建」结论继续有效）；内嵌浏览器同时解决采集和未来工作台；托盘/更新/密钥存储/通知/多窗口生态最全；`desktop/` 独立 package.json，不污染 `app/web` |
| 缺点 | 安装包大；每个房间一个 renderer，内存要限流；第三方页面是安全边界，必须 `sandbox:true` / `contextIsolation:true` / 禁 `nodeIntegration` / 权限默认拒绝；引入 Node 构建链（只在外壳目录）；签名与更新要投入 |
| 什么时候选 | **默认推荐**。尤其是确定要做「AI 工具集/工作台」——内嵌浏览器就是工作台的底座 |

### 方案 D · Tauri 2 + WebView2

**形态**：Rust 主进程 + 系统 WebView2；Python engine 作为 sidecar（PyInstaller 出的 exe）；
多 webview 用 Tauri 2 的 `WebviewWindow` / 子 webview。

| 项 | 值 |
|---|---|
| 内置浏览器 | 系统 WebView2，多 webview 可以嵌；但不是自带 Chromium |
| 直播间 | 同样靠注入 `captureStream()`，但 WebView2 的后台播放/静音/权限行为要重新 spike |
| 安装体积 | ~400–500MB（省掉 Chromium） |
| 工作量 | **20–30 人天**，Rust + 多 webview 管理 + sidecar 生命周期 |
| 优点 | 安装包小、内存低；Rust 侧安全边界清晰；Windows 上 WebView2 基本预装 |
| 缺点 | 内核版本随用户系统变，采集这种「靠浏览器行为吃饭」的功能最怕这个；多视图/DevTools/权限要自己补；跨平台（未来 mac）要重验 WebKit；生态和示例远少于 Electron |
| 什么时候选 | 体积是硬指标，且愿意为「内核不可控」承担采集回归风险 |

### 方案 E · 扩展 / PWA 伴生（不做主壳）

**形态**：本地服务 + 安装包；用户浏览器装 MV3 扩展负责采集，UI 用 PWA 安装。

| 项 | 值 |
|---|---|
| 内置浏览器 | 用户自己的浏览器 |
| 采集 | 扩展体验其实最好：真标签页、真登录态、每页点一次授权 |
| 工作量 | 6–10 人天 |
| 优点 | 采集最贴近平台真实环境；不需要打包 Chromium |
| 缺点 | 不是桌面应用；Chrome 137+ 不能命令行加载扩展，用户要手动开发者模式；上架商店有权限审核风险；Python 服务仍要单独安装 |
| 什么时候选 | **不作为主路径**，但可以作为 C 的「专业采集模式」长期保留：内嵌采不到的平台，退回扩展 |

---

## 5. 对比表

| | A 轻壳 | B pywebview | **C Electron** | D Tauri | E 扩展/PWA |
|---|---|---|---|---|---|
| 应用内多标签浏览器 | ✗ | ✗ | **✓** | ✓（WebView2） | ✗ |
| 现有 UI 改动 | 0 | 0 | **0** | 0 | 0 |
| 采集链路改动 | 0 | 0 | C1 0 / C2 注入 | 注入 + 重验 | 扩展 |
| 安装体积 | 450–600MB | 400–550MB | 650–800MB | 400–500MB | 小（但要装服务） |
| 内存（3 路浏览器采集） | 同现在 | 同现在 | 同现在 + 壳 ~200MB | 略低 | 同现在 |
| 工作量 | 3–5 人天 | 6–10 人天 | **4–6 周** | 20–30 人天 | 6–10 人天 |
| 工作台延展 | 弱 | 中 | **强** | 强 | 无 |
| 主要风险 | 不像应用 | 能力天花板 | 包大、安全面 | 内核不可控 | 不是应用 |

---

## 6. 推荐路线

**P0（1 周）· 打包底座** —— 其实就是方案 A 的交付物：

- PyInstaller onedir 出 `engine.exe`；`%LOCALAPPDATA%` 数据目录；模型/ffmpeg 随包；
- 一个启动器负责拉起 server / agent / bot、托盘、单实例、日志、退出清理；
- 验收：在一台没装 Python 的 Windows 上，双击安装、打开浏览器、录一场、导出、退出无残留。

**P1（1.5–2 周）· Electron 壳（C1）**：

- 主窗口加载 localhost UI；托盘/菜单/通知/自动更新；engine 由 Electron 守护；
- 采集仍走外部浏览器；
- 验收：关窗口进托盘、二次启动聚焦、升级不丢数据、杀掉 engine 能自动重启。

**P2（2–3 周）· 内嵌浏览器（C2）**：

- `WebContentsView` 标签条；每房间一个视图；注入采集；共享登录分区；静音；
- 先做 2–3 天 spike，只验四件事：`captureStream` 可用、静音不影响、后台不断流、单视图内存；
- 验收：抖音 direct 失败后一键切内嵌；非抖音链接内嵌可采；3 路内嵌 + 2 路 direct 稳定 1 小时；
  关房间后进程/内存归零。

**P3（持续）· 工作台**：

- 工具页注册机制（每个工具一个页面/视图）；API 密钥进 `safeStorage`（Windows DPAPI）；
- 中转站统一代理（主进程发请求，绕开 CORS，统一日志/计费/重试）；
- 生图等第三方 UI 直接内嵌成视图；后台任务 + 通知 + 全局快捷键。

**不做什么**（避免走回头路）：

- 不把 UI 重写成 SPA/组件框架——docs/11 的结论仍然成立，桌面壳不需要它；
- 不用 CEF/cefpython——维护成本高、资料少，Electron 已经把这件事做完了；
- 不在 Electron 里重写 Python 后端——模型、VAD、声纹、SQLite 都留在 engine；
- 不把 Node 工具链塞进 `app/web`——`desktop/` 独立，`python run.py` 永远是开发主路径。

---

## 7. 对「AI 工作台」的延展

壳/引擎分离之后，未来的工作台是自然的：

```
┌─ 壳（Electron）──────────────────────────────┐
│  导航：直播 | 逐字稿 | 生图 | 中转站 | 设置    │
│  每个工具 = 一个页面或一个 WebContentsView     │
│  密钥、通知、更新、快捷键、托盘               │
└──────────────┬───────────────────────────────┘
               │ 只走 HTTP API（127.0.0.1）
┌──────────────▼───────────────────────────────┐
│  引擎（Python）：采集、ASR、声纹、总结、存储    │
│  未来可拆：生图/中转站也可以放引擎或独立服务    │
└──────────────────────────────────────────────┘
```

两个长期判断：

1. **引擎才是资产，壳可以换。** 现在 `--lan` + 手机回放已经证明 UI 可以离开本机；
   以后引擎跑在小主机/NAS，桌面端只是瘦客户端，这条路是通的。打包时不要把 UI 和引擎焊死。
2. **内嵌浏览器是工作台的入口，不只是采集的补丁。** 中转站后台、生图工具、第三方控制台，
   都可以是「一个视图 + 一次登录」。这也是为什么推荐 C 而不是 B。

---

## 8. 一句话总结

- 「开浏览器标签页」不是打包的障碍：**自己的页面进主窗口，直播间进内嵌视图，登录/支付交给系统浏览器**。
- 内嵌浏览器反而能干掉现在采集链路里的扩展、非公开参数、每房间独立 profile 三个历史包袱。
- 先做 A 拿到打包底座，再上 C1 壳，最后 C2 内嵌；direct 采集始终优先，内嵌只做兜底。
- 唯一需要提前投入验证的，是 **Electron 视图里的 `captureStream` + 静音 + 后台播放 + 内存** 这四件事；
  2–3 天 spike 能给出答案，再决定 C2 是否全量铺开。
