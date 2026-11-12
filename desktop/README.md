# LiveNotes 桌面壳（Tauri 2）

这是 [docs/20](../docs/20-tauri-2-adr.md) 的 spike 实现：Rust 壳负责窗口、托盘、
引擎进程守护和 WebView2 房间视图；Python 引擎、`app/web` 和采集底座不改。
失败检测、日志打包与一键导出见 [docs/21](../docs/21-desktop-diagnostics.md)。

## 目录

```
desktop/
  dist/                  # 壳自己的本地页
    shell.html           # 默认主窗口：活动栏 / 对象列表 / 舞台 / 检查器
    shell.css / shell.js # 单窗口壳样式与交互
    diagnostics.html     # 诊断状态、断流/崩溃事件、导出入口
    browser.html         # 应用内浏览器：地址栏、多标签、采集状态
  src-tauri/src/
    main.rs              # 入口
    lib.rs               # Builder、托盘、单实例、窗口
    engine.rs            # 选端口、拉起引擎、健康检查、退出回收
    http.rs              # loopback HTTP 小客户端（不引入 reqwest）
    rooms.rs             # WebView2 子视图、captureStream 注入、PCM 回传、静音
    diag.rs              # 失败事件、托盘状态、看门狗、诊断包
  test/
    run_m1_spike.py      # 壳视图驱动与截图（m1 / m1local / m1shell）
    run_flow_check.py    # 添加/重试/停止/命令面板流程验收
    run_capture_check.py # 后台采集不弹第二个窗口
    run_exit_check.py    # 正常退出：引擎/helper 零残留
    run_hardkill_check.py# 硬杀壳：Job Object 收掉整棵进程树
    run_adopt_check.py   # 接管已有引擎，退出时一起收掉
    run_close_check.py   # 关窗三选弹窗 / 托盘 / 退出设置
    run_p1_p2_check.py   # 工作台内切场次 / 批量 / 回收站 / 清理中心 / 右键菜单
    site_server.py       # 本地测试站：/room/a、/room/b、真实 <video> + 音轨
    make_media.ps1       # SAPI 合成中文语音 + ffmpeg 生成 speech.mp4
    verify_diag.py       # 断流/房间崩溃/引擎退出与诊断包验收
  web/                   # 桌面皮肤（overlay）：改这里不会影响网页版
    host.json            # 宿主清单：加载哪些 CSS/JS、能力、文案
    css/paper.css        # 纸面主题（覆盖 app/web 的 CSS 变量与表面色）
    css/desktop.css      # 应用内右键菜单 / 全库搜索 / 轻提示
    js/desktop.js        # 上述交互 + data-ln-copy 文案覆盖
  build.cmd              # 进入 MSVC 环境后跑 cargo
  run-dev.cmd            # 进入 MSVC 环境后跑 tauri dev
  run-dev.ps1            # 开发态包装：退出时收掉 tauri dev 整棵进程树
```

## 工具链

- Visual Studio 2022 Community，工作负载 **使用 C++ 的桌面开发**
  （`Microsoft.VisualStudio.Workload.NativeDesktop`，包含 MSVC v143 + Windows 11 SDK）；
- Rust stable-msvc（`rustup default stable-msvc`）；
- Node 20+ / npm；
- WebView2 Runtime（Windows 10/11 一般自带）。

Rust 装在仓库上级的 toolchains 目录（脚本会自动定位；也可用 %USERPROFILE%）：

```bat
set RUSTUP_HOME=%USERPROFILE%\toolchains\rustup
set CARGO_HOME=%USERPROFILE%\toolchains\cargo
set PATH=%CARGO_HOME%\bin;%PATH%
```

`build.cmd` / `run-dev.cmd` 已经处理了这两件事，并会先调用 `vcvars64.bat`，
确保 `link.exe` 是 MSVC 的而不是 Git Bash 自带的 GNU `link`。

## 开发

```bat
cd desktop
npm install
run-dev.cmd
```

开发态壳会从 `CARGO_MANIFEST_DIR/../..` 找到仓库根的 `run.py`，用
`python run.py` 启动引擎，端口由壳选空闲端口，数据目录是
`%LOCALAPPDATA%\LiveNotes\data`，日志在 `%LOCALAPPDATA%\LiveNotes\logs`。

也可以用 `LIVENOTES_PYTHON` 指定 Python，或用 `LIVENOTES_ENGINE_EXE` 指定一个
已打包的 `engine.exe`。

## 单窗口工作区（M1–M4）

默认主窗口是本地壳 `dist/shell.html`（`tauri.localhost`），不再是引擎页：

```
顶栏 / 活动栏 / 对象列表 / 主画布 / 检查器 / 状态栏
```

- **收听**：房间列表 + 信号画布 + 监控墙 + 实时字幕检查器；添加、重试、单路停止、
  停止全部都在窗口内完成；
- **工作台 / 文件 / 人物 / 设置**：现有引擎页以 `?embed=1` 装进内容区，页面顶栏隐藏；
  工作台默认选中第一个有发言的场次；
- **按需页面**：直播页只在点「显示页面」时以原生 WebView2 子视图落到舞台；
  命令面板 / 确认框打开前会先隐藏子视图；
- **命令面板**：`Ctrl+K`；`Ctrl+1..5` 切换一级视图；
- **后台采集**：`browser` 采集模式下，待启动房间由不可见的采集控制台接管，
  不再弹出第二个「应用内浏览器」窗口。

页面之间的桥是 `app/web/js/embed.js`：同源链接与快捷键通过 `postMessage` 交给壳，
壳再用 Tauri IPC 管原生子视图。旧入口可回退：

```bat
set LIVENOTES_LEGACY_UI=1
run-dev.cmd
```

实现细节、验收截图和已知限制见 [docs/25](../docs/25-desktop-workspace-implementation.md)。
三个验收脚本：

```bash
python -I desktop/test/run_m1_spike.py m1shell   # 视图切换 + 截图
python desktop/test/run_flow_check.py           # 添加/重试/停止/命令面板
python -I desktop/test/run_capture_check.py     # 后台采集不弹窗
```

## Web 分层：基座 + 桌面皮肤

网页基座 `app/web` 和桌面皮肤是两套目录，不再靠「改共享 CSS」实现桌面效果：

```
app/web/                     基座：标准网页版 + 宿主适配器
  js/embed.js                ?embed=1 时只做协议（标记、主题、链接/快捷键回传、载入清单）
  css/embed.css              嵌入模式让位（藏顶栏、设置页铺满）
  css/index.css              冷色 token（--spk-* / --cat-*）
desktop/web/                 皮肤：只有 CSS/JS/清单，没有页面
  host.json                  css/js 清单 + capabilities + copy 文案
```

- **开发态**：壳拉起引擎时自动加 `--web-overlay desktop/web`（见 `engine.rs`），
  所以 `run-dev.cmd` 起来的桌面版带皮肤，`python run.py` 仍是纯网页版；
- **打包态**：`package-engine.py` 把 `desktop/web` 复制成资源里的
  `app/web-overlay`，引擎自己发现并优先出这一层，不需要壳传参；
- **改哪里**：桌面观感/文案/壳内交互改 `desktop/web/**`，网页版零影响；
  基座改版只要保留 `data-ln-region` 锚点和 `--spk-*` / `--cat-*` 变量，皮肤就不会崩。

契约、锚点表和消息协议见 [docs/29](../docs/29-page-host-contract.md)。
提交前跑这两条（CI 里也应该跑）：

```bash
python tools/check_web_layers.py     # 基座不许出现桌面标记；皮肤只能用锚点
python -m pytest tests/test_web_layers.py -q
```

## D2 / D3 手工验收

1. 起测试站：

   ```bash
   python desktop/test/site_server.py
   ```

2. 托盘菜单 →「多房间采集（D2/D3）」；
3. 点「建会话并打开 A」：房间 A 加载 `http://127.0.0.1:8899/room/a`，
   页面右上角应出现「LiveNotes · 采集中」，控制台状态栏应看到
   `n_utterances` 增长；
4. 点「打开 B（验证共用登录）」：B 页应显示「已共享登录」，
   `shell.log` 里两条 `room room-b ... cookie=demo_login=alice` 就是证据；
5. 点「静音 A」：扬声器无声，但控制台的 `last_chunk_ago_s` 和字幕继续更新；
6. 点「打开实时字幕」：现有 `app/web/live.html` 显示实时字幕，页面代码未改。

实时字幕窗口的自动回归（不点鼠标也能验证「窗口真的加载、壳没卡死」）：

```bash
python desktop/test/site_server.py          # 另开一个终端
set LIVENOTES_SPIKE=live
run-dev.cmd
```

`run-dev.cmd` 会先打开应用内浏览器、加载 `http://127.0.0.1:8899/room/a`，
12 秒后走真实按钮路径打开字幕窗口。`shell.log` 里应依次出现
`live_open_view ok`、`live: main thread responsive`、`live: main thread responsive after close`；
`engine.log` 里应能看到字幕页每 2 秒一次的 `GET /api/live/list`。
同步命令建窗会把主线程堵死：症状是窗口白屏、壳关不掉，`live: main thread blocked` 就是回归信号。

## 故障诊断（D6）

桌面用户没有「看控制台」这条路，所以壳自己检测三类失败并在托盘提示：
引擎退出/假死、房间视图进程崩溃、直播断流。托盘第一项状态可直接打开诊断页
（`dist/diagnostics.html`）；失败时图标带红色角标，右键菜单可
「导出诊断包…」「打开日志目录」。

- 日志：`%LOCALAPPDATA%\LiveNotes\logs\{shell.log,diag.jsonl,engine.log}`
- 诊断包：桌面 `LiveNotes-diagnostics-YYYYMMDD-HHMMSSZ.zip`，含 summary、
  事件、引擎快照、日志尾部、WebView2 dump；不含 settings.json、LLM Key、
  录音、字幕正文
- 引擎已经退出时仍可导出；包里的 HTTP 快照会记录 `{"error": ...}`

验收：

```bash
python desktop/test/verify_diag.py
```

脚本用测试站起壳，CDP 真崩一次房间视图，再关掉采集制造断流，最后杀掉引擎，
从壳日志、`diag.jsonl`、引擎 HTTP 和导出的 zip 四路交叉验证。
需要先构建 debug 壳（`desktop\build.cmd build`）；如果用了自定义
`CARGO_TARGET_DIR`，用 `--shell <exe>` 或 `LIVENOTES_SHELL_EXE` 指定。

可调开关：`LIVENOTES_DIAG_DIR`（导出目录）、`LIVENOTES_SHELL_STALL_SECONDS`
（钉死断流阈值）、`LIVENOTES_SHELL_POLL_MS`（轮询间隔）、
`LIVENOTES_DIAG_AUTO_EXPORT=1`（失败时自动导出，仅自动化用）、
`LIVENOTES_SPIKE=d3crash`（自动崩溃驱动）。细节见
[docs/21](../docs/21-desktop-diagnostics.md)。

## 应用内浏览器（D7）

托盘 →「应用内浏览器」。地址栏粘贴链接，回车开新标签页；页面在子 WebView2 里播放，
`captureStream()` 的 PCM 经 `chrome.webview.postMessage` 回传，和 D2/D3 是同一条链路。

- 前端：`dist/browser.html`；窗口：`rooms::open_browser_console`；
- 切换标签页用 `room_show(visible)`（`Webview::show/hide`），不销毁视图；
- 关窗口时 `close_all_rooms` 会关闭子视图，并对每个会话调用 `/api/live/stop`；
- 每个标签页一个会话；所有标签页共用 `webview2/rooms` 登录目录；
- 主窗口 `window.open`（收听/文件中心/人物/阅读/高光榜）：同源引擎链接由壳在
  内容区打开，不再开第二个窗口；外部链接按设置页「桌面版 → 新窗口打开方式」
  进应用内浏览器或系统浏览器（`prefs.open_mode`）。`browser` 采集模式下，
  这个窗口默认**不可见**，只作为后台采集宿主。

自动验收（先起测试站）：

```bash
python desktop/test/site_server.py
```

另开一个 `cmd` 终端：

```bat
set LIVENOTES_SPIKE=browser
set LIVENOTES_AUTO_EXIT_MS=110000
run-dev.cmd
```

运行中执行 `python desktop/test/verify.py d3`，应看到两个会话都在出块、至少一个有字幕。
真实抖音/快手页面的登录与反爬尚未验收。

并行调试时如果已经有一个壳在跑，可加 `LIVENOTES_ALLOW_MULTI=1` 跳过单实例锁
（只对 debug 构建生效，release 始终单实例）。

## 关窗、退出与残留回收

关窗不等于退出。设置页「桌面版 → 关闭窗口时」决定没有录音时的行为：
`tray`（默认，隐藏到托盘继续录音）或 `quit`（直接退出）。有房间在录音时，
点 X 或托盘「退出 LiveNotes」都会先弹三选：取消 / 最小化到托盘 / 停止并退出。

进程层做了三件事，壳无论怎么死都不留孤儿：

- **Job Object**：引擎和它拉起的飞书机器人、采集代理、Chrome/ffmpeg
  挂在 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job 里，壳进程消失时由内核回收；
- **父进程看门狗**：`LIVENOTES_PARENT_PID` + `app/runtime.py`，每 2 秒探一次父进程；
- **单实例与接管**：`data/runtime/engine.lock` + `engine.json`；新壳启动先探活，
  健康就复用同一个引擎，不健康的残留引擎确认是本应用进程后回收。

开发态用 `run-dev.cmd`（内部走 `run-dev.ps1`），退出时会收掉 `tauri dev`
的 node/cargo 进程树。

验收脚本：

```bash
python desktop/test/run_exit_check.py       # 正常退出零残留
python desktop/test/run_hardkill_check.py   # 硬杀壳，Job Object 兜底
python desktop/test/run_adopt_check.py      # 接管已有引擎
python desktop/test/run_close_check.py      # 三选弹窗 / 托盘 / 退出设置
```

细节、根因和已知限制见 [docs/27](../docs/27-process-lifecycle.md)。

## 飞书机器人与采集方式（桌面端自动）

桌面壳启动引擎时设 `LIVENOTES_HELPERS=1`，`run.py` 会顺带拉起
`tools/feishu_bot.py`（有凭证时）：飞书长连接，贴链接就建房间。
端口、`LIVENOTES_API`、`LIVENOTES_BASE_URL`、`LIVENOTES_DATA_DIR` 都指向桌面引擎，
所以飞书消息里的回听/分享链接也是 `127.0.0.1:<桌面端口>`。

房间怎么采，跟设置页「桌面版 → 采集方式」（`prefs.capture_mode`）走：

| 值 | 谁接房间 | 说明 |
|---|---|---|
| `browser`（默认） | 桌面壳 `start_room_watcher` | 建会话 + 应用内浏览器采集标签页，不弹外部浏览器 |
| `direct` | `tools/capture_agent.py` + `direct_capture.py` | 抖音直取，ffmpeg 拉 PCM；不开浏览器 |
| `external` | `tools/capture_agent.py` | 网页版同款，每房间一个 Chrome |

切换由引擎里的 `_watch_capture_mode` 处理：有房间在听时等它们结束后再生效。
`browser` 模式下壳不拉采集代理；`direct`/`external` 下壳的 watcher 跳过 pending 房间。

- 凭证：`%LOCALAPPDATA%\LiveNotes\data\feishu.json`；开发态缺文件时从仓库
  `data/feishu.json` 复制一份（`engine.log` 里搜 `[helpers]`）。
- 打包态：`engine.exe --feishu-bot` / `--capture-agent` 复用同一个入口；
  `package-engine.py` 已把三个脚本、`websockets` 和 `app.*` 依赖打进 resources。
- 网页版不设 `LIVENOTES_HELPERS`，仍由 `tools/start.bat` 分别拉起；
  同一个飞书机器人不要同时开两套。
- 关掉：`LIVENOTES_NO_FEISHU=1` 只关机器人，`LIVENOTES_NO_HELPERS=1` 全关。

## 设计取舍

- **引擎 onedir**：PyInstaller 产物放 `resources/engine/`，不走 `externalBin`
  （onefile 启动慢、杀软敏感）。
- **PCM 传输**：注入脚本 → `window.chrome.webview.postMessage` → Rust 校验
  （房间、块大小、每秒块数）→ `POST /api/live/chunk`。页面不碰本地端口，
  不受直播页 CSP/CORS 影响。
- **房间 profile**：所有房间共用 `%LOCALAPPDATA%\LiveNotes\webview2\rooms`，
  一次登录所有房间共用；工具页以后各自独立目录。
- **z-order**：WebView2 子视图是原生子窗口，会盖在 HTML 上。控制台把按钮放在
  顶部固定条，子视图从 y=132 开始；弹窗/下拉需要先隐藏视图。
- **静音**：`ICoreWebView2Controller::SetIsMuted`，在输出层静音，不影响
  `captureStream()` / RMS。
- **浏览器入口**：同源引擎链接由单窗口壳在内容区打开；外部链接按
  `prefs.open_mode` 进应用内浏览器或系统浏览器。`browser` 采集模式由壳的
  room watcher 接住，采集控制台默认隐藏；`direct` / `external` 仍由引擎侧
  采集代理接管。

## 打包（D5）

```bat
python desktop/package-engine.py --with-models --with-ffmpeg
desktop\build-release.cmd
```

产物：

- `desktop/src-tauri/resources/engine/engine.exe`（onedir，随 resources 走）
- `desktop/src-tauri/target/release/bundle/nsis/LiveNotes_0.1.0_x64-setup.exe`

2026-11-10 实测量级：壳 10.3MB、engine 166MB、models 257MB、ffmpeg 212MB，
resources 634.5MB，NSIS 278.33MiB。

验收脚本（自己起引擎、推真实语音、查进程树）：

```bash
python desktop/test/verify_packaged.py --engine "desktop/src-tauri/resources/engine/engine.exe"
python desktop/test/verify_packaged.py --install-dir "desktop/.build/d5install5" --seconds 30
```

注意：

- Tauri 把 `bundle.resources` 原样拷贝到 `<exe目录>\resources\`；`engine.rs` 优先找
  这个子目录，再回退 `<exe目录>\`。
- 静默安装要在 `cmd` 里用 `start /wait`；从 Git Bash 直接执行 GUI 安装器会
  立刻返回 0，实际安装还没结束。
- 安装器与 `engine.exe` 目前未签名，正式发布前必须签名；SmartScreen/杀软会拦。
- `src-tauri/examples/wv2probe.rs` 是 WebView2 环境探针，example 不会打进安装包。
