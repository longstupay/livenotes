# 22 · 桌面版使用教程（Tauri 2）

- **适用版本**：`desktop/`（Tauri 2 壳，D1–D7 + 单窗口工作区 M1–M4）
- **面向**：想直接使用桌面版的人，以及第一次在本机构建它的人
- **相关文档**：[docs/20 决策记录](20-tauri-2-adr.md)、[docs/21 故障诊断](21-desktop-diagnostics.md)、[docs/25 单窗口工作区](25-desktop-workspace-implementation.md)、[desktop/README](../desktop/README.md)

---

## 0. 先搞清楚：Tauri 2 在这里是什么

它不是一个新的 App，而是给现有网页版套的一层 **Windows 原生外壳**：

```
LiveNotes.exe（Rust + Tauri 2）
  ├─ 启动页 / 错误页 / 诊断页 / 单窗口壳（desktop/dist/*.html）
  ├─ 托盘图标与菜单
  ├─ 拉起 Python 引擎（开发态 python run.py；打包态 resources/engine/engine.exe）
  └─ 主窗口 = 本地壳，引擎页在内容区 iframe（http://127.0.0.1:<随机端口>/）
```

所以你**不需要**单独开浏览器、单独起服务；双击桌面版，它自己把引擎拉起来。
Python、模型、ffmpeg 在安装包里都带着（打包态），用户机器上不用装 Python。

### 两套启动方式并存

桌面版**不替代**原来的网页版，两套都保留、功能都要完整：

- **网页版**：`python run.py`，浏览器打开 `http://127.0.0.1:8765/`；标签页采集、扩展、
  direct 直取、飞书机器人全按原方式工作。
- **桌面版**：`LiveNotes.exe`，自己拉引擎、自带托盘和内嵌浏览器；不要求用户装
  Python 或浏览器。
- 两套共用同一套 Python 引擎和 `app/web`；桌面端只是多了一层壳和一个自己的采集入口。

桌面端默认在**单窗口工作区**里听：左栏房间、主画布信号/页面、右栏实时字幕，
不再为了「收听」弹第二个窗口。`browser` 采集模式下，采集控制台在后台隐藏运行；
需要手动开页面时才用托盘 →「应用内浏览器」。

### 当前仓库的状态

| 东西 | 有没有 |
|---|---|
| 壳源码 `desktop/src-tauri` | ✅ 有 |
| 前端依赖 `desktop/node_modules` | ❌ 没有，要先 `npm install` |
| 打包引擎 `desktop/src-tauri/resources/engine` | ❌ 没有（只有 `.gitkeep`） |
| 安装包 `.../bundle/nsis/*.exe` | ❌ 没有，要自己构建 |

也就是说：**现在还不能"下载一个 exe 双击安装"**，第一次必须先跑起来或构建一次。

---

## 1. 三种用法，先选一种

| 你想干什么 | 用哪种 | 需要装工具链吗 | 耗时 |
|---|---|---|---|
| 改代码、看效果、调界面 | **开发态**（`run-dev.cmd`） | 需要（VS C++、Rust、Node） | 首次编译 5–20 分钟 |
| 自己日常用，不想每次开命令行 | **构建安装包**，装到电脑 | 需要一次 | 首次构建 10–30 分钟 |
| 发给同事 / 换一台电脑用 | **构建安装包**，把 setup.exe 拷过去 | 打包机需要 | 同上，且要带引擎资源 |

> 只装了安装包的电脑**不需要** Python、Rust、VS、Node；需要 WebView2 Runtime
> （Windows 10/11 一般自带，没有就装微软的 Evergreen Runtime）。

---

## 2. 开发态：不安装，直接从源码跑

### 2.1 工具链检查（本机已具备）

| 依赖 | 检查命令 | 本机状态 |
|---|---|---|
| Visual Studio 2022 + C++ 桌面开发 | 用 `vswhere` 找 `Microsoft.VisualStudio.Component.VC.Tools.x86.x64`；Community / Professional / Enterprise / Build Tools 均可 | ✅ 本机 Community |
| Rust stable-msvc | `cargo --version`；脚本按 `CARGO_HOME` → 仓库上级 `toolchains/` → `%USERPROFILE%\.cargo` 查找 | ✅（脚本自动定位） |
| Node 20+ / npm | `node --version` / `npm --version` | ✅ Node 24 / npm 11 |
| WebView2 Runtime | `C:\Program Files (x86)\Microsoft\EdgeWebView\Application` | ✅ |
| Python 引擎依赖 | `python -c "import sherpa_onnx, numpy"` | ✅ |
| 本地模型 | `models/` 下有 `sensevoice/ speaker/ vad/` | ✅ |

`desktop/build.cmd`、`run-dev.cmd`、`build-release.cmd` 会先调用 `env.cmd`：
自动找 VS（`vswhere`，找不到再按常见版本目录兜底）、按 `CARGO_HOME` →
仓库上级 `toolchains/` → rustup 默认目录的顺序找 Rust，并把 `CARGO_BUILD_JOBS`
限成 2（防内存打爆）。**不要绕过脚本直接 `cargo build`**，否则容易用到 Git Bash
自带的 GNU `link.exe`。

### 2.2 第一次运行

在 `cmd`（不是 Git Bash）里：

```bat
cd /d C:\path\to\livenotes\desktop
npm install
run-dev.cmd
```

或者直接在资源管理器里双击 `desktop\run-dev.cmd`。

- `npm install` 只装一个东西：`@tauri-apps/cli`，但第一次要联网。
- `run-dev.cmd` 第一次会下载并编译 Rust 依赖，**很慢且吃内存**；这期间别同时开
  多个浏览器/构建。之后再启动通常几十秒。
- 启动顺序：小启动页（520×300）→ 托盘出现 → 主窗口加载网页版。
  引擎健康检查最长等 75 秒，首次加载模型时可能停十几秒，属正常。

### 2.3 开发态下引擎怎么跑

壳会在仓库根执行：

```
python run.py
```

并注入这些环境变量（不用你手动设）：

| 变量 | 值 |
|---|---|
| `LIVENOTES_PORT` | 壳挑的空闲端口（不是固定 8765） |
| `LIVENOTES_HOST` | `127.0.0.1`（桌面版只监听本机） |
| `LIVENOTES_DATA_DIR` | `%LOCALAPPDATA%\LiveNotes\data` |
| `LIVENOTES_NO_BROWSER` | `1`（不自动开浏览器） |
| `TMP` / `TEMP` | `%LOCALAPPDATA%\LiveNotes\data\tmp` |

想换 Python 解释器：

```bat
set LIVENOTES_PYTHON=C:\path\to\python.exe
run-dev.cmd
```

想换一个已经打好的引擎：

```bat
set LIVENOTES_ENGINE_EXE=C:\path\to\engine.exe
run-dev.cmd
```

### 2.4 停止

- 托盘右键 →「退出 LiveNotes」：有房间在录音时会先弹确认（取消 / 最小化到托盘 /
  停止并退出）；确认后壳会一并结束引擎和 helper 进程树。
- 点主窗口右上角 X：**没有录音时**按设置页「桌面版 → 关闭窗口时」执行
  （默认最小化到托盘，也可选退出程序）；**有录音时**先弹三选，不会静默退出。
- 或者在跑 `run-dev.cmd` 的窗口按 `Ctrl+C`；`run-dev.ps1` 会收掉
  `tauri dev` 的 node/cargo 进程树。
- 调试时可用 `LIVENOTES_CLOSE_TO_TRAY=0` 跳过 X 的拦截（直接关窗）。

进程回收细节见 [docs/27](27-process-lifecycle.md)。

### 2.5 改代码后要重启

- 改 Rust / `dist/*.html`：`tauri dev` 会重新编译并刷新壳。
- 改 `app/` 下的 Python 或 `app/web/` 页面：壳不会热重载引擎，**退出重开**才生效。

---

## 3. 打包成安装包：真正"装到电脑上"

### 3.1 先把引擎打进 resources

```bat
cd /d C:\path\to\livenotes
python desktop\package-engine.py --with-models --with-ffmpeg
```

产物：

```
desktop/src-tauri/resources/engine/engine.exe   ← PyInstaller onedir
desktop/src-tauri/resources/models/...
desktop/src-tauri/resources/ffmpeg/ffmpeg.exe
```

- 不跑这一步也能 `tauri build` 成功，但装到**别的电脑**上会因为找不到引擎而起不来；
  在开发机上它会回退到仓库根的 `run.py`，掩盖问题。
- 大约 634MB 资源；确保磁盘有空间。
- 模型没下过就先 `python tools/fetch_models.py`；ffmpeg 不在 PATH 就去掉
  `--with-ffmpeg` 或先装一个。

### 3.2 构建安装包

```bat
cd /d C:\path\to\livenotes\desktop
npm install
build-release.cmd
```

产物：

```
desktop\src-tauri\target\release\bundle\nsis\LiveNotes_0.1.0_x64-setup.exe
```

2026-11-10 实测量级：NSIS 安装包 **278.33 MiB**，壳本体 10.3MB。

### 3.3 安装

1. 双击 `LiveNotes_0.1.0_x64-setup.exe`；
2. 安装模式是 **currentUser**，装到当前用户目录，**不需要管理员权限**；
3. 安装器**未签名**，Windows SmartScreen 会拦：点「更多信息」→「仍要运行」；
   部分杀软也可能误报，这是未签名 + PyInstaller 的已知情况，正式发布前必须签名；
4. 装完从开始菜单找 **LiveNotes** 启动。

> 静默安装要在 `cmd` 里用 `start /wait`。从 Git Bash 直接执行 GUI 安装器会立刻
> 返回 0，实际安装还没结束。

### 3.4 卸载

Windows「设置 → 应用 → 已安装的应用 → LiveNotes → 卸载」。
卸载**不会**删数据目录 `%LOCALAPPDATA%\LiveNotes`（录音、设置都在里面），
确认不要了再手动删。

---

## 4. 日常使用

### 4.1 启动后你会看到

1. 一个小启动页，标题「LiveNotes / 直播语记」；
2. 任务栏右下角出现托盘图标（蓝色小图标）；
3. 引擎就绪后主窗口打开：默认是**单窗口工作区**（活动栏 + 房间/场次列表 + 主画布 + 检查器），
   不再是整页网页版。旧界面可用 `LIVENOTES_LEGACY_UI=1` 回退。

如果引擎起不来，启动页会换成「LiveNotes · 引擎未启动」错误页，托盘仍然在——
**托盘是引擎挂了以后唯一还能用的入口**。

### 4.2 托盘菜单

| 菜单项 | 作用 |
|---|---|
| 第一行状态（如「运行正常」） | 点它打开**诊断页**；失败时变成「引擎已退出 / N 路断流」 |
| 导出诊断包… | 在**桌面**生成 `LiveNotes-diagnostics-YYYYMMDD-HHMMSSZ.zip`，并打开所在文件夹 |
| 打开日志目录 | 打开 `%LOCALAPPDATA%\LiveNotes\logs` |
| 显示主窗口 | 把主窗口调回前台（双击托盘图标同效） |
| 停止全部录音 | 停止当前所有在听的直播间（不退出应用） |
| 应用内浏览器 | 打开标签页式内嵌浏览器：地址栏 + 多标签 + 采集状态。单窗口壳的采集控制台默认隐藏，这个入口用于手动开页面/排查（引擎就绪前是灰的） |
| 多房间采集（D2/D3） | 打开 WebView2 多房间控制台（引擎就绪前是灰的） |
| 在浏览器中打开 | 用系统默认浏览器打开桌面版的引擎地址（同一个本地服务、同一份数据）；实时监听走这里；收听页的采集代理是独立进程，桌面内多路采集请用「多房间采集」（引擎就绪前是灰的） |
| 退出 LiveNotes | 退出应用并回收引擎；有录音时先弹确认 |

单实例：重复双击快捷方式不会开第二个，只会把已有窗口调到前台。

#### 4.2.1 应用内浏览器与采集控制台

日常监听不需要打开这个窗口：在单窗口工作区左栏点「＋」粘贴链接，主画布显示状态，
采集由壳在后台的应用内采集控制台完成。

托盘 →「应用内浏览器」用于手动开页面或排查。地址栏粘贴直播间链接，回车即开新标签页：

- 页面在应用内的 WebView2 里播放，音频通过 `captureStream()` 直接进引擎，**不需要外部 Chrome/Edge**；
- 每个标签页一个采集会话；切换标签页只隐藏视图，不停止采集（后台参数已开）；
- 工具栏：后退/前进/刷新、静音（不影响采集）、实时字幕、停止并关闭；
- 所有标签页共用 `%LOCALAPPDATA%\LiveNotes\webview2\rooms` 登录目录；
- 手动关闭这个窗口会停止其中标签页的采集；平时它由壳隐藏托管，不要手动结束进程。

主窗口里的同源链接（收听 / 文件中心 / 人物 / 阅读 / 高光榜）由壳在内容区打开，
不再新开窗口。外部链接按设置页「桌面版 → 新窗口打开方式」进应用内浏览器或系统浏览器。
网页版（`python run.py`）不显示这个选项，行为不变。

> 「实时监听」不是 `window.open`，它要浏览器的标签页选择器，仍然需要托盘
> 「在浏览器中打开」后在 Chrome/Edge 里用；这个设置管不到它。
>
> 桌面内新增直播间采集，优先用壳左栏「＋」或托盘「多房间采集（D2/D3）」。

本地验收先用测试站：

```bash
python desktop/test/site_server.py
```

然后打开 `http://127.0.0.1:8899/room/a`。真实抖音/快手页面的登录与反爬还没验收，
见 [docs/20](20-tauri-2-adr.md) 的待办。

旧界面（`LIVENOTES_LEGACY_UI=1`）里网页顶栏的「文件中心 / 人物 / 阅读 / 高光榜 / 收听」
仍是 `window.open`；壳会按设置把它们交给应用内浏览器或系统浏览器打开，地址仍是
桌面版的本地引擎，所以看到的是同一份数据。「实时监听」依赖浏览器的标签页选择器，
桌面窗口里选不到你外部 Chrome 的标签页；需要它时用托盘「在浏览器中打开」，
再在 Chrome/Edge 里点「● 实时监听」。

#### 4.2.2 飞书机器人（桌面端自动拉起，后台采集）

桌面版启动引擎时会一并拉起 `tools/feishu_bot.py`（有凭证时）：飞书长连接、
贴链接建房间、`/状态`、`/stop`、高光推送等命令照旧。

**房间由桌面壳自己接住**：壳每 2 秒看一次 `/api/rooms`，发现飞书或收听页新建的
房间，就建一个采集会话，并在**隐藏的采集控制台**里开标签页——不弹外部 Chrome，
也不弹第二个可见窗口。第一块音频到达后房间才变「监听中」，飞书里的「开好了我喊你」
不会提前喊。如果设置里把「采集方式」改成抖音直取或外部 Chrome，房间就交给对应的
采集代理，壳不再抢（见 4.2.3）。

- 飞书 `/stop`、收听页停止：房间落到 `stopped`，对应标签页和会话一起收掉；
- 壳会在主窗口的收听工作区显示这一路的状态、时长和实时字幕；
- 托盘 →「应用内浏览器」可以手动打开同一个采集控制台，用于登录/排查；
- 想看网页版那套外部 Chrome 采集，可设 `LIVENOTES_DESKTOP_CAPTURE_AGENT=1` 回退
  （不设就是默认的应用内浏览器）。

凭证放在桌面数据目录：

```
%LOCALAPPDATA%\LiveNotes\data\feishu.json
```

开发态第一次启动时，如果这个文件不存在，引擎会从仓库 `data\feishu.json` 复制一份
（`engine.log` 里会有 `[helpers] 已复制飞书凭证 …`）。打包态没有仓库 `data\`，
需要手动把 `feishu.json` 放进上面的目录后重启桌面端。

注意：

- 桌面版和网页版的飞书机器人共用同一套凭证，**不要同时开两套**：飞书长连接会把
  消息分给其中一条，另一条收不到。
- 桌面版采集用的是应用内 WebView2 的登录目录
  `%LOCALAPPDATA%\LiveNotes\webview2\rooms`；第一次采集抖音/快手需要先在
  应用内浏览器里登录一次。
- 不想让桌面端拉起飞书机器人：启动前设 `LIVENOTES_NO_FEISHU=1`；
  后台进程都不想要：设 `LIVENOTES_NO_HELPERS=1`。
- 相关日志：`engine.log` 搜 `[helpers]`，`shell.log` 搜 `room watcher`。

#### 4.2.3 采集方式（桌面版设置）

主窗口 ⚙ 设置 →「桌面版」→「采集方式」：

| 选项 | 行为 | 依赖 |
|---|---|---|
| 应用内浏览器（默认） | 房间在应用内 WebView2 标签页采集，不弹外部浏览器 | WebView2 |
| 抖音直取 | `tools/direct_capture.py` 解析抖音流，ffmpeg 直接拉 PCM；不开浏览器 | ffmpeg（打包自带），streamlink 可选 |
| 外部 Chrome | 网页版同款采集代理，每个房间一个 Chrome | Chrome / Edge |

- 直取目前只做抖音；快手 / B 站等会按「直取失败处理」停在收听页，等你确认
  （确认切换会用外部 Chrome），或者直接把采集方式改回「应用内浏览器」。
- 有房间正在听时切换会等它们结束后生效；新房间才用新方式。
- 网页版不显示这个选项，仍由 `tools\start.bat` 和 `LIVENOTES_CAPTURE_MODE` 决定。
- 相关日志：`engine.log` 里搜 `采集方式` / `采集模式`。

### 4.3 数据、日志、缓存在哪

| 内容 | 路径 |
|---|---|
| 数据（录音、SQLite、设置） | `%LOCALAPPDATA%\LiveNotes\data` |
| 日志 | `%LOCALAPPDATA%\LiveNotes\logs\shell.log`、`diag.jsonl`、`engine.log` |
| WebView2 用户数据 | `%LOCALAPPDATA%\LiveNotes\webview2\` |

注意：**桌面版的数据目录和仓库里的 `data/` 是两个地方**。网页版（`python run.py`）
用的是仓库 `data/`；桌面版用的是 `%LOCALAPPDATA%\LiveNotes\data`。所以第一次打开
桌面版看不到你以前的录音和设置，这是正常的。

想把旧数据带过去：退出 LiveNotes，把仓库 `data\` 里的内容复制到
`%LOCALAPPDATA%\LiveNotes\data\`（会覆盖同名文件，先备份）。

### 4.4 诊断

托盘状态变红/带角标时：

1. 点托盘第一行打开诊断页，看引擎、断流、房间崩溃事件；
2. 托盘 →「导出诊断包…」，把桌面上的 zip 发给开发；
3. 诊断包**不含** `settings.json`、LLM Key、录音、字幕正文，可以放心外发。

更多阈值与自动化验收见 [docs/21](21-desktop-diagnostics.md)。

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 双击安装包没反应 / 被拦 | 未签名，SmartScreen | 「更多信息」→「仍要运行」；杀软加信任 |
| 启动页停很久 | 首次加载模型；引擎健康检查最长 75 秒 | 等；超过 75 秒看 `engine.log` |
| 启动页一闪就退出 / `shell.log` 有 `splash build failed` | WebView2 的启动页 profile 被上次崩溃的残留进程占住 | 新版会自动换 `splash-<pid>` 目录重试一次；仍失败就结束残留的 `msedgewebview2.exe` 后重开 |
| 主窗口显示「引擎未启动」 | Python 依赖缺失、模型缺失、端口/权限问题 | 托盘导出诊断包；看 `engine.log` 末尾；开发态先手动 `python run.py` 验证 |
| 托盘有图标但没窗口 | 主窗口被关或最小化 | 双击托盘 / 右键「显示主窗口」 |
| 顶栏「文件中心 / 人物 / 阅读 / 高光榜 / 收听」点了没反应 | 旧版壳没有处理 `window.open`，Tauri 默认静默拒绝 | 更新到含单窗口壳的版本；同源链接默认在内容区打开，也可在设置里切系统浏览器 |
| 收听页里添加直播间没反应 | 旧版桌面壳没有拉起采集宿主 | 更新到含单窗口工作区的版本；左栏「＋」添加后由壳的后台采集控制台接管 |
| 飞书里发链接，机器人不回 / 不开听 | 旧版桌面端只起引擎，没有起机器人；或凭证不在桌面数据目录 | 重启桌面端，看 `engine.log` 的 `[helpers]` 行；把 `feishu.json` 放到 `%LOCALAPPDATA%\LiveNotes\data` |
| 应用内浏览器打开了页面但没字幕 | 页面没有正在播放的 `<video>/<audio>`，或还没点播放 | 先在页面里点播放；看标签页圆点和状态栏的块数；用测试站先验证链路 |
| 应用内浏览器里抖音打不开 / 要验证 | WebView2 不是完整 Chrome，站点可能检测内嵌 | 先用托盘「在浏览器中打开」兜底；真实直播页的内嵌验收还没做完 |
| 「实时监听」选不到直播间标签页 | 它是浏览器标签页采集，WebView2 不共享外部 Chrome 的标签页 | 托盘「在浏览器中打开」，在 Chrome/Edge 里监听；桌面内采集用「多房间采集」 |
| 构建时报 `link.exe` 相关错误 | 直接跑了 `cargo`，用到 GNU link | 只用 `build.cmd` / `run-dev.cmd` / `build-release.cmd` |
| `cargo` 找不到 | 没设 `CARGO_HOME` | 用脚本；或手动 `set CARGO_HOME=%USERPROFILE%\toolchains\cargo` 并加 `%CARGO_HOME%\bin` 到 PATH |
| 构建时内存爆、进程被 Killed | 16GB 机器多路并行编译 | 脚本已限 `CARGO_BUILD_JOBS=2`；关掉其他大程序 |
| 端口被占 | 壳每次挑空闲端口 | 不用处理；想看当前端口，查 `shell.log` 的 `engine ready on 127.0.0.1:<port>` |
| 换台电脑装完打不开 | 没打包 `resources/engine` | 回到 3.1 重新 `package-engine.py` 后再构建 |
| 手机打不开桌面版的页面 | 桌面版只监听 `127.0.0.1` | 手机访问请用网页版 `python run.py --lan` |

---

## 6. 自检与验收（可选）

```bat
:: 构建 debug 壳
desktop\build.cmd build

:: D6 故障诊断：断流、房间崩溃、引擎退出、诊断包
python desktop\test\verify_diag.py

:: 打包态验收：自己起引擎、推真实语音、查进程树
python desktop\test\verify_packaged.py --engine "desktop\src-tauri\resources\engine\engine.exe"
```

多房间（D2/D3）手工验收步骤见 [desktop/README](../desktop/README.md)。

---

## 7. 最短路径总结

**桌面版里监听直播间（不依赖外部浏览器）：**

单窗口工作区 → 左栏「＋」→ 粘贴链接 → 回车。采集在后台运行，主画布看信号/页面，
右栏看实时字幕。要手动开页面：托盘 →「应用内浏览器」。

**旧网页版（保留，不受桌面端影响）：**

```bash
python run.py
```

**桌面版今天就想用（不安装）：**

```bat
cd /d C:\path\to\livenotes\desktop
npm install
run-dev.cmd
```

**想装到电脑上长期用：**

```bat
cd /d C:\path\to\livenotes
python desktop\package-engine.py --with-models --with-ffmpeg
cd desktop
npm install
build-release.cmd
```

然后双击 `desktop\src-tauri\target\release\bundle\nsis\LiveNotes_0.1.0_x64-setup.exe`。
