# 20 · 决策记录：采用 Tauri 2 作为桌面壳

- **状态**：已接受（2026-11-10）
- **决策人**：项目负责人
- **取代**：[docs/19](19-electron-vs-tauri.md) 第 7 节的「选 Electron」建议；
  docs/19 的对比与数据保留，作为本次决策的依据。
- **范围**：只影响桌面壳；Python 引擎、Web UI、采集底座不变。

---

## 1. 决策

A2 桌面端采用 **Tauri 2（Windows 优先）**：

- **壳**：Rust + Tauri 2，负责窗口、托盘、更新、进程守护、WebView2 视图管理。
- **引擎**：现有 Python 服务用 PyInstaller 打成 `engine.exe`（onedir），作为随包资源，
  由壳拉起、守护、退出时回收。
- **UI**：继续使用 `app/web` 下的零构建静态页；壳的主窗口加载
  `http://127.0.0.1:<port>/`，页面代码不改。
- **采集**：`direct`（ffmpeg）仍然优先；需要浏览器时用 WebView2 子视图注入
  `captureStream()`，不再依赖外部 Chrome/Edge 和 MV3 扩展。
- **工具**：每个功能一个静态页 + manifest + Engine API 命名空间；壳只负责挂载和隔离。

---

## 2. 为什么可以选 Tauri 2

在承认代价的前提下，Tauri 2 对 A2 是成立的：

| 利好 | 说明 |
|---|---|
| 壳小、启动快 | 壳约 10–20MB，不随包带 Chromium；启动不拖累引擎 |
| Windows 上就是 Chromium | WebView2 是 Edge 内核（本机 157），`captureStream()`、H.264、DevTools 都在 |
| 安全默认更好 | capability 模型、无 Node、Rust 内存安全；远程页面默认拿不到 IPC |
| 原生控制够细 | `with_webview` 能拿到 `ICoreWebView2Controller`，静音、CDP、权限都能做 |
| 跨平台留口 | 未来 macOS/Linux 有官方路径（虽然采集要重验） |
| 移动端可选 | Tauri 2 支持 iOS/Android；当前不需要，但不堵死 |

**我们明确接受的代价**（不是忽略，是记账）：

1. WebView2 常青，版本不可完全锁定；采集行为可能随 Edge 更新漂移。
2. 多视图的静音、CDP、权限、后台播放要自己落 Rust + COM，代码量比 Electron 多。
3. 没有 Chrome 扩展；现有 `extensions/livehub` 不能原样复用。
4. Tauri updater 没有差分更新，默认全量下载；600MB 的包每次更新都很大。
5. macOS/Linux 是另外两套内核，采集要分别验证。
6. 仓库多一门 Rust；构建链从「Python + 静态页」变成「Python + Rust + Node」。

这些代价都有对应的缓解措施，见第 8 节。

---

## 3. 架构落位

```
┌─ Tauri Shell（Rust，薄）──────────────────────────────────┐
│  主窗口：加载 http://127.0.0.1:<port>/（现有静态页）       │
│  房间/工具：WebView2 子视图（每个一个独立 data_directory） │
│  托盘、单实例、自启、通知、全局快捷键、深链               │
│  进程守护：engine.exe 的启动/健康检查/重启/退出回收        │
│  更新：tauri-plugin-updater（壳与引擎分开升级）            │
│  密钥：Windows Credential Manager 或 Engine DPAPI          │
└───────────────────────┬────────────────────────────────────┘
                        │ 只走 HTTP/WS/SSE（127.0.0.1 + token）
┌───────────────────────▼────────────────────────────────────┐
│  Engine（Python，厚，保持不变）                            │
│  采集 / ASR / 声纹 / 总结 / SQLite / 任务队列 / 调度       │
│  Provider 适配层：LLM、生图、中转站、重试/配额/计费        │
│  工具 API：/api/tools/<tool-id>/...（按 scope 鉴权）       │
└────────────────────────────────────────────────────────────┘
```

**沿用 docs/19 第 5 节的六条契约**：Engine 是唯一数据源；工具只认 HTTP；
每个工具一个 manifest；每个工具一个隔离分区；打开才挂载、关闭即销毁；API 带版本。

### 3.1 目录草案

```
desktop/
  package.json                 # 只放 @tauri-apps/cli 与前端构建（可选）
  src-tauri/
    Cargo.toml
    tauri.conf.json
    capabilities/              # 主窗口、房间远程页、工具页各自的权限
    src/
      main.rs                  # 启动、托盘、单实例、窗口
      sidecar.rs               # engine 进程管理
      room.rs                  # 房间 WebView2 视图
      capture_win.rs           # 静音/权限/后台参数等 Windows 专用
    resources/
      engine/                  # PyInstaller onedir 产物（gitignore）
      models/                  # 257MB，随包或首启下载
      ffmpeg/                  # 静态构建
  README.md                    # 开发/构建/打包说明
```

`app/web` 不动；`python run.py` 仍是开发主路径。

---

## 4. Windows 关键实现点

> 以下 API 名称以 Tauri 2 当前版本为准，spike 时逐条确认签名。

### 4.1 引擎进程与端口

- Rust 启动时选一个空闲端口（`TcpListener::bind("127.0.0.1:0")`），
  通过 `LIVENOTES_PORT` / 命令行参数传给 `engine.exe`；
- 设置 `LIVENOTES_DATA_DIR=%LOCALAPPDATA%\LiveNotes\data`；
- `CREATE_NO_WINDOW` 启动，避免黑窗口；
- 轮询 `/api/health`（或现有任一轻量端点）直到 200，再加载主窗口；
- 崩溃重启：指数退避，最多 3 次，之后托盘提示「引擎未启动」；
- 退出：`RunEvent::ExitRequested` / `Exit` 时先发停止信号，超时再 kill 进程树；
- 日志：engine stdout/stderr 落到 `%LOCALAPPDATA%\LiveNotes\logs`。

### 4.2 主窗口

- 生产：`WebviewUrl::External("http://127.0.0.1:<port>/")`；
- 开发：`python run.py --reload` 起 8765，Tauri 直接加载 8765，壳只改窗口；
- 单实例：第二次启动聚焦已有窗口（`tauri-plugin-single-instance`）。

### 4.3 房间视图（核心）

- 每个房间一个子 webview：`WebviewBuilder` + `window.add_child`；
- 房间共享一个 `data_directory`（`persist:live` 等价物），**一次登录所有房间共用**；
  工具页则各自独立目录；
- 窗口 resize 时同步 `set_position` / `set_size`；
- 切换标签时 `set_focus`，关闭时 `close` 并等待销毁；
- **z-order 注意**：WebView2 子视图是原生子窗口，会盖在 HTML 之上。
  打开对话框/下拉菜单时，先隐藏对应视图，关闭后再显示；或者把登录/支付类操作
  放到独立窗口。这是 Tauri 多视图最常见的 UX 坑。
- 页面导航：`on_navigation` 只允许直播域名与本地页面；`window.open` 一律拦截，
  需要时交给系统浏览器。
  （已实现：主窗口注册 `on_new_window`；桌面默认把 http/https 新窗口交给应用内
  浏览器作为工具页（`capture=false`，不建采集会话），设置页「桌面版 → 新窗口打开方式」
  可切回系统浏览器。托盘另提供「在浏览器中打开」，用于实时监听这类需要真实浏览器
  标签页的流程。D7 又加了正式的应用内浏览器窗口：`dist/browser.html` + 托盘
  「应用内浏览器」，地址栏、多标签、`Webview::show/hide` 切换，采集走同一条
  `captureStream` 链路。）

### 4.4 采集注入与 PCM 传输

推荐链路：

```
页面 <video> --captureStream()--> AudioContext(16k) --ScriptProcessor(4096)-->
  int16 PCM --Tauri IPC 命令 live_chunk--> Rust --HTTP POST--> Engine /api/live/feed
```

为什么不直接让页面 POST 本地服务：

- 直播页可能有 CSP，`connect-src` 未必允许 `127.0.0.1`；
- 走 IPC 可以统一做大小/频率校验，不把本地端口暴露给页面脚本。

Tauri 2 的远程页面默认不能调 IPC；需要给直播域名单独的 capability，
只放行 `live_chunk` 一个命令，并校验：

- 每个房间一个短期 token（壳生成，Engine 校验）；
- 单块大小上限、每秒块数上限；
- 只接受 PCM 字节，不接受任意 JSON/文件路径。

若 spike 发现远程 capability 配置不顺，退路是：注入脚本直接
`fetch('http://127.0.0.1:<port>/api/live/feed')`（loopback 被视为可信来源，
但要看页面 CSP），或经本地 WebSocket。三条路在 spike 里选一条。

### 4.5 静音

```rust
webview.with_webview(|w| {
    #[cfg(windows)]
    unsafe {
        let controller = w.controller(); // ICoreWebView2Controller
        controller.SetIsMuted(true).ok();
    }
});
```

验证点：静音后扬声器无声，但 `captureStream()` 的 RMS 不变
（现有扩展已证明 Chromium 的静音在输出层，不影响媒体元素采集）。

### 4.6 后台播放

给 WebView2 加启动参数（`additional_browser_args` 或
`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`）：

```
--autoplay-policy=no-user-gesture-required
--disable-background-timer-throttling
--disable-renderer-backgrounding
--disable-backgrounding-occluded-windows
```

验证点：最小化/切到其他标签 10 分钟，`/api/live/status` 的 `last_chunk_at`
持续更新，不触发 120 秒断流看门狗。

### 4.7 权限与下载

- WebView2 `PermissionRequested`：默认拒绝摄像头/麦克风/地理位置/通知；
- 下载：直播页不应触发下载；如需下载，走 Rust 侧保存到用户目录；
- 外链：`on_navigation` 白名单之外的 URL 交给 `tauri-plugin-opener` / 系统浏览器。

### 4.8 托盘、自启、通知、快捷键

| 能力 | 方案 |
|---|---|
| 托盘 | `tauri::tray::TrayIconBuilder` |
| 单实例 | `tauri-plugin-single-instance` |
| 开机自启 | `tauri-plugin-autostart` |
| 系统通知 | `tauri-plugin-notification` |
| 全局快捷键 | `tauri-plugin-global-shortcut` |
| 深链 | `tauri-plugin-deep-link` |
| 日志 | `tauri-plugin-log` |

### 4.9 密钥

现状：LLM Key 存在 `data/settings.json`（明文风险）。桌面化时至少做到：

- 方案 A（推荐，改动小）：Engine 用 Windows DPAPI 加密后存
  `%LOCALAPPDATA%\LiveNotes\secrets.dat`（Python 通过 `ctypes` 调
  `CryptProtectData` / `CryptUnprotectData`，不引入 pywin32）；
- 方案 B：Rust 侧用 Windows Credential Manager 存，Engine 启动时通过环境变量/一次性
  管道拿 Key（不要把 Key 写进命令行参数，会出现在进程列表里）。

### 4.10 更新

Tauri updater 默认全量下载，没有 electron-updater 那种 blockmap 差分。
对策：

1. **拆包**：壳（10–20MB）单独更新；Engine 作为独立组件（版本号、下载、校验、替换）；
   models/ffmpeg 只在必要时更新。
2. **壳更新**：`tauri-plugin-updater` + minisign 签名 + OSS/R2 上的 `latest.json`；
3. **Engine 更新**：Engine 自带 `/api/version`，壳启动时比较远端 manifest，
   下载 zip、校验 sha256、解压到版本目录、切换软链/目录指针；
4. 若拆包成本太高，先接受「全量更新」，但把 models 放到用户数据目录，
   避免每次更新都重下 257MB。

---

## 5. 打包

| 项 | 做法 |
|---|---|
| Engine | PyInstaller **onedir**，整个目录放 `resources/engine/`；Rust 用资源路径 spawn，不走 `externalBin`（onefile 启动慢、杀软敏感） |
| 模型 | `resources/models/` 随包；或首启下载到 `%LOCALAPPDATA%` |
| ffmpeg | `resources/ffmpeg/`；注意 GPL/LGPL 再分发义务，优先 LGPL build |
| WebView2 | 默认 `downloadBootstrapper`；离线安装包用 `embedBootstrapper` 或 `offlineInstaller` |
| 安装器 | NSIS；安装后约 550–700MB，压缩后约 280–380MB |
| 数据 | `%LOCALAPPDATA%\LiveNotes\data`；首次启动可导入旧 `data/` |
| 签名 | 所有 exe 签名；否则 SmartScreen/杀软会拦 PyInstaller 产物 |
| 卸载 | 保留用户数据目录，询问是否删除录音 |

---

## 6. 本机工具链就绪度（2026-11-10 实测）

| 项 | 状态 | 说明 |
|---|---|---|
| Node / npm | ✅ 24.20 / 11.19 | 可装 `@tauri-apps/cli` |
| WebView2 Runtime | ✅ 157.0.4341.42 | 采集内核就位 |
| Rust / cargo / rustup | ❌ 未安装 | 需要 rustup + stable-msvc |
| VS 2022 C++ 工作负载 | ❌ 未安装 | `vswhere -requires VC.Tools` 无结果 |
| Windows SDK | ❌ 未见 | `Windows Kits\10\Lib` 不存在 |
| PyInstaller | ❌ 未安装 | 打包阶段再装 |
| C 盘可用 | 24GB | 装 VS C++ 工作负载 + SDK 偏紧 |
| E 盘可用 | 207GB | 仓库与 `target/` 放这里 |

> **2026-11-10 spike 后更新**：VS 2022「使用 C++ 的桌面开发」、Windows SDK
> 10.0.26100、Rust stable-msvc 1.99.1、PyInstaller 6.22.3 均已装好并跑通；
> 下表保留当时的初始盘点，作为决策依据。

**结论：先装工具链，才能起 spike。** 顺序：

1. Visual Studio 2022 Community 增加「使用 C++ 的桌面开发」工作负载
   （MSVC v143 + Windows 11 SDK）；或用 Build Tools 单独装。
   安装位置尽量放 E 盘，或先清出 15GB C 盘空间。
2. `rustup` 安装 stable-msvc：
   `winget install Rustlang.Rustup`，然后 `rustup default stable-msvc`。
3. `npm install` 装 `@tauri-apps/cli`（在 `desktop/` 下）。
4. 验证：`rustc -V`、`cargo -V`、`cargo tauri -V`、MSVC `link.exe` 可用。

> 16GB 内存 + WSL 4GB 的机器上，Rust 首次链接会比较吃内存：
> 构建前关掉 WSL/多余的 Chrome，或设 `CARGO_BUILD_JOBS=2`。

---

## 7. Spike 计划（5 天，先验证再铺开）

| 天 | 做什么 | 通过标准 |
|---|---|---|
| D1 | 工具链 + 最小 Tauri 窗口 + 拉起 `python run.py` + 托盘/退出回收 | 双击壳能起引擎、加载首页、退出无残留进程 |
| D2 | 子 webview 打开一个直播间；共享登录目录；resize/切换/关闭 | 两个房间共用登录；关闭后 WebView2 进程回落 |
| D3 | 注入 `captureStream` → IPC → Engine `/api/live/feed`；静音 | 页面出现实时字幕；静音后无声但 RMS 不变 |
| D4 | 后台/最小化 10 分钟；3 路 + 2 路 direct 1 小时；崩溃重启 | 不断流；内存 <3.5GB；杀掉视图能重建 |
| D5 | PyInstaller onedir + NSIS 安装包 + 干净虚拟机安装 + 更新包体积 | 无 Python 环境可运行；数据目录正确；更新策略可接受 |

**任一硬门槛不过，先停，不要继续写业务。**

### 7.1 Spike 结果（2026-11-10）

全部在 Windows 10 Pro 19045、WebView2 157.0.4341.42、16GB 内存的本机完成。
取证方式是外部观察器：读壳日志、Engine HTTP、进程工作集，不依赖壳自报成功。

| 项 | 结果 | 关键证据 |
|---|---|---|
| D1 壳起引擎 / 退出回收 | **PASS** | 引擎在 `127.0.0.1:52282` 就绪，`/api/health` 200、`/` 200；`RunEvent::Exit` 后 `stopping engine pid=...` / `engine killed`，无 `engine.exe` 残留 |
| D2 多房间共享登录 | **PASS** | 房间 A/B 都读到 `demo_login=alice`；关闭 A/B 后 WebView2 进程 19 → 12，渲染进程 3 → 2 |
| D3 实时字幕 + 静音 | **PASS** | 静音后 `SetIsMuted=true`；RMS 前 `0.04531` / 后 `0.04504`（29 个采样）；字幕 3 → 18 条；最大出块间隔 0.3s |
| D4 1 小时 3+2 路 | **PASS** | 5 路会话（3 个 WebView2 房间 + 2 路 direct）跑满 3600s；工作集峰值 **2082.8MB**（门槛 3500MB）；每路最大出块间隔 **0.3s**，无断流；最小化 600s 期间 5 路继续出块；第 1500s 关闭并重建 room-c 后复用原 session，4s 恢复 `capturing` |
| D5 打包 / 安装 / 无 Python | **PASS（干净 VM 待复核）** | `resources` 634.5MB，NSIS 安装包 **278.33MiB**，壳本体 10.3MB；安装到隔离目录后从 `resources/engine/engine.exe` 启动，真实语音产出 3 条字幕；子进程 PATH 只留系统目录、无 `python.exe`；数据写入 `%LOCALAPPDATA%\LiveNotes\data`；退出后引擎无残留 |

**D4 参数**：`LIVENOTES_SPIKE=d4`，最小化 600s，1500s 重建 room-c，自动退出 3720s；
两路 direct 各推 14297 个 4096-sample 块，零错误；观察器每 15s 采一次，共 231 个样本。
复跑命令：

```bash
python desktop/test/verify.py d4 --seconds 3600 --interval 15 --expect 5
```

**D5 产物与包体**：

| 组件 | 大小 | 说明 |
|---|---:|---|
| 壳 `livenotes-desktop.exe` | 10.3MB | Rust + Tauri 2.12.5，release |
| `resources/engine/` | 166MB | PyInstaller onedir；含 `_internal/app/web`、sherpa-onnx、lark-oapi、websockets |
| `resources/models/` | 257MB | SenseVoice 239.2MB + CAM++ 28.3MB + VAD 0.6MB |
| `resources/ffmpeg/ffmpeg.exe` | 212MB | gyan.dev 9.0 full build |
| `resources` 合计 | 634.5MB | 安装后磁盘约 645MB |
| NSIS 安装包 | **278.33MiB** | LZMA 压缩；`installMode=currentUser` |

安装态复跑命令：

```bash
python desktop/test/verify_packaged.py --install-dir "desktop/.build/d5install5" --seconds 30
```

**D5 暴露的问题 / 待办**：

1. **资源路径**：Tauri 把 `resources/**/*` 原样落在 `<exe目录>\resources\`，
   壳原先只找 `<exe目录>\engine`，打包后必然找不到引擎。已改为优先
   `<resource_dir>\resources\`，再回退 `<resource_dir>\`。
2. **`wv2probe.exe` 误打包**：`src/bin/wv2probe.rs` 被 Tauri 当成第二个 bin 打进安装包，
   并导致 CLI 找不到主程序。已移到 `src-tauri/examples/`，最终包不再包含。
3. **引擎包偏大**：`--collect-all comtypes` 把 pytest 测试也收进来，166MB 里有可裁部分。
   `pycaw`/`comtypes` 只服务旧的 `tools/capture_agent.py`，不在打包引擎的运行路径上，
   下一轮可去掉这两项，预计再减 30–50MB。
4. **ffmpeg 212MB**：当前是 full build；换成 LGPL shared build 可显著缩小，
   同时把 GPL/LGPL 再分发义务说清楚。
5. **签名**：安装包和 `engine.exe` 均未签名，SmartScreen/杀软会拦；
   正式发布前必须签名（尤其是 PyInstaller 产物）。
6. **sherpa-onnx 日志噪声**：1 小时里出现 3276 行
   `circular-buffer.cc ... Invalid n:`，但字幕全程正常、无丢句；属于上游 VAD 缓冲日志，
   后续跟进版本或降日志级别，不作为硬门槛。
7. **干净 VM 未实测**：本机没有可用的 Windows Sandbox/Docker。当前用「隔离安装目录 +
   独立 `LOCALAPPDATA` + 只含系统目录的 PATH」替代，已验证不依赖本机 Python；
   真正的无 Python 干净虚拟机安装仍需在一台新机器上复核。
8. **更新体积**：278MB 全量包不适合高频更新。建议壳（10MB）、引擎（166MB）、
   models（257MB）、ffmpeg（212MB）拆开升级；models/ffmpeg 只在必要时更新，
   或首次启动下载到 `%LOCALAPPDATA%`。Tauri updater 没有差分，默认全量。
9. **静默安装**：从 Git Bash 直接执行 NSIS 安装器会立刻返回 0（GUI 安装器未被等待），
   必须用 `start /wait` 或安装器自身的交互进程确认完成；自动化脚本要注意这一点。

**结论**：D1–D4 硬门槛全部通过；D5 的「安装后可运行、不依赖本机 Python、
数据目录正确」已在本机隔离环境验证，唯一未闭环的是干净 VM 与签名。
可以继续铺开，但发布前必须补干净 VM 复验、代码签名和拆包更新。

---

## 8. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 | 验证 |
|---|---|---|---|---|
| WebView2 静音/后台行为不符合预期 | 中 | 高 | D3/D4 spike；参数组合；必要时改「外部浏览器兜底」 | 硬门槛 |
| 远程页面拿不到 IPC / CSP 阻挡 | 中 | 高 | 三条传输路线任选；最差回退外部浏览器 | D3 |
| 子视图 z-order/焦点问题 | 高 | 中 | 弹窗时隐藏视图；登录走独立窗口；UI 规范 | D2 |
| Tauri 更新全量下载 | 高 | 中 | 壳/引擎/模型拆包；或自建差分 | D5 |
| Rust + COM 代码难维护 | 中 | 中 | 只在一个 `capture_win.rs` 里封 COM；其余用 Tauri 高层 API | 代码评审 |
| WebView2 版本漂移导致采集回归 | 中 | 高 | 采集回归脚本；监控 WebView2 版本；可选固定运行时 | 持续 |
| PyInstaller 被杀软误报 | 中 | 中 | 签名；onedir；提交误报；安装包白名单 | D5 |
| C 盘空间不足 | 高 | 中 | VS 组件装 E 盘；`target/` 放 E；清缓存 | D1 |
| 16GB 内存构建/采集吃紧 | 中 | 中 | 限制并发；direct 优先；关 WSL | D4 |

---

## 9. 回退与重估触发

1. **D3/D4 采集硬门槛不过**：保留 Tauri 壳，但采集继续走外部专用浏览器
   （产品仍成立，只是少一个内嵌能力）；或把壳换回 Electron，
   Engine 与工具契约不变（docs/19 第 5 节的价值就在这里）。
2. **更新体积不可接受**：先拆 Engine/模型；仍不行再评估 Electron 差分更新。
3. **未来必须支持 macOS**：WebKit 的采集行为要重新验证；届时重新评估壳。
4. **WebView2 连续两个版本导致采集回归**：考虑固定运行时（包体 +150MB）或换壳。

---

## 10. 下一步

Spike 已完成：D1–D4 全部通过，D5 本机隔离安装通过、干净 VM 待复核。接下来：

1. **收尾 D5**：在一台没有 Python/WebView2 的干净 Windows 10 上跑安装器；
   同时验证 WebView2 的 `downloadBootstrapper` 与 `offlineInstaller` 两条路径。
2. **减包**：去掉 `comtypes`/`pycaw` 的 `--collect-all`，ffmpeg 换 LGPL shared build；
   目标安装包 < 200MB、安装后 < 450MB。
3. **签名与更新**：签名壳和 `engine.exe`；接 `tauri-plugin-updater`；
   按 7.1 的拆包建议做壳 / 引擎 / 模型 / ffmpeg 分离升级。
4. **工程化**：把 spike 的 `LIVENOTES_*` 测试开关收进 feature/测试构建，
   清理 `boot.log` 等调试落盘；房间视图接入真实直播页并做导航白名单。
5. **产品化**：工具页 manifest + Engine API 命名空间、macOS/Linux 评估，
   以及 docs/18 路线图的 P1/P2 改写。
6. **应用内浏览器（D7）**：`desktop/dist/browser.html` + 托盘「应用内浏览器」已加入，
   支持地址栏、多标签、切换时 `show/hide` 子视图、采集状态、静音、实时字幕；
   主窗口 `window.open` 按 `prefs.open_mode` 路由（默认应用内工具页，可切系统浏览器）；
   `LIVENOTES_SPIKE=browser` 可自动开两个标签页验收。真实直播页与导航白名单
   仍按第 4 条继续。

---

## 附：与 docs/18、docs/19 的关系

- docs/18 的五种方案与打包清单仍然有效，只是最终壳从 Electron 换成 Tauri 2。
- docs/19 的对比数据保留；本文档是对其建议的**有意推翻**，不是遗忘。
- 如果未来回退 Electron，docs/18 的 C1/C2 路线可以直接复用。
