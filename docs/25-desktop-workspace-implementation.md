# 25 · 桌面单窗口工作区：M1–M4 实现说明

- **状态**：已实现（2026-11-11），工作区未提交
- **关联**：[docs/23 交互重构方案](23-desktop-ux-redesign.md)、[docs/24 M1 Spike 报告](24-m1-spike-report.md)、[docs/20 Tauri 2 ADR](20-tauri-2-adr.md)
- **范围**：桌面壳（`desktop/`）与引擎页的嵌入适配；Engine API、ASR、总结链路未重写

---

## 0. 结论先行

桌面端现在默认是**一个窗口**：

- 主窗口加载本地壳 `desktop/dist/shell.html`（`tauri.localhost`），不再把引擎页直接当主窗口；
- 收听、工作台、文件中心、人物、设置、实时字幕都在同一个窗口里切换；
- 直播画面只在需要时以**原生 WebView2 子视图**落到舞台矩形，默认看信号画布；
- `browser` 采集模式不再弹第二个「应用内浏览器」窗口，采集控制台在后台隐藏运行；
- `Ctrl+K` 命令面板、监控墙、按需页面、添加 / 重试 / 停止 / 停止全部全部可用；
- 旧入口仍可回退：`LIVENOTES_LEGACY_UI=1` 启动原引擎页主窗口。

---

## 1. 交付范围

| 里程碑 | 交付 |
|---|---|
| **M1 · 单窗口壳** | 顶栏 / 活动栏 / 对象列表 / 主画布 / 检查器 / 状态栏；本地壳走 Tauri IPC；引擎页在内容区 iframe；独立浏览器窗口不再是主流程 |
| **M2 · 收听工作区** | 房间列表（状态、时长、异常原因）、信号画布、监控墙、实时字幕检查器、添加直播间、单路停止 / 重试、停止全部、全局「N 路在听」；后台采集宿主 |
| **M3 · 工作台与资料** | 场次列表 + 工作台 / 文件中心 / 人物 / 设置嵌入同一壳；默认选中「有内容」的场次；`embed=1` 隐藏引擎页顶栏；iframe 内链接、`Ctrl+K` / `Ctrl+1..5` 通过 `postMessage` 交给壳 |
| **M4 · 增强** | 命令面板（键盘导航）、监控墙添加入口、按需页面模式（原生子视图占满舞台）、页面内 `window.open` 收口到壳 |

---

## 2. 窗口结构

```
主窗口（http://tauri.localhost/shell.html）
├── topbar        面包屑 / 命令面板入口 / N 路在听 / 设置
├── rail 64px     收听 · 工作台 · 文件 · 人物 · 设置
├── sidebar 280   当前模式的上下文列表（房间 / 场次）
├── canvas        信号画布 / 监控墙 / 原生直播页面子视图
│   └── frameView 工作台 / 文件中心 / 人物 / 设置（iframe，带深色画框）
├── inspector 340 当前房间的诊断、会话、实时字幕
├── statusbar     引擎端口 · 房间数 · 在听数 · 关窗提示
└── overlays      命令面板 / 确认框 / toast
```

### 视图与路由

壳内不引入路由库，`switchView(view, opts)` 直接管理状态：

| view | 内容 | 说明 |
|---|---|---|
| `listen` | 收听工作区 | 信号画布 / 监控墙 / 原生页面 |
| `workbench` | 场次与字幕 | `/?embed=1&session=<sid>`，默认选第一个有发言的场次 |
| `files` | 文件中心 | `/library?embed=1` |
| `people` | 人物档案 | `/people?embed=1` |
| `settings` | 设置 | `/?embed=1&settings=1`，整页铺满画框 |
| `live` | 实时字幕 | `/live?embed=1&session=<sid>`，深色页不套画框 |
| `frame` | 按需页面 | 任意引擎页，标题 / 返回由壳提供 |

---

## 3. 关键交互

### 3.1 收听工作区

- 左栏按 `在听 → 启动中 → 异常 → 已停止` 排序，行内显示状态点、时长、异常原因；
- 选中房间后主画布默认是信号画布：波形、CH 编号、统计、错误诊断与重试；
- **监控墙**：2×2 / 3 列自适应卡片，每张卡片一条迷你波形；最后一格是「添加一路直播间」；
- **添加**：左栏 `＋` 或命令面板「添加直播间」，粘贴链接回车；走 `POST /api/rooms`；
- **停止**：检查器底部「停止」→ 二次确认 → `POST /api/rooms/close`；
- **重试**：异常房间的画布与检查器都有入口 → `POST /api/rooms/state {state:"pending"}`；
- **实时字幕**：检查器轮询 `/api/live/status`；非 live 房间回退到 `/api/utterances`，历史场次也能看；
- 顶栏「N 路在听」常驻，点开在信号画布 / 监控墙之间切换。

### 3.2 命令面板

`Ctrl+K` 打开；支持上下键、Enter、Esc、点击。动作包括：

- 收听 / 工作台 / 文件中心 / 人物 / 设置；
- 添加直播间、打开监控墙；
- 当前房间的显示页面 / 实时字幕；
- 停止全部采集、刷新数据。

### 3.3 工作台与资料

- 场次列表来自 `/api/sessions`，默认选中第一个 `n_utterances > 0` 的场次；
- 工作台、文件、人物、设置都通过 `iframe` 复用现有页面，`embed.js` 隐藏页面自己的顶栏；
- iframe 内的同源链接被 `embed.js` 拦截，`postMessage` 给壳，由壳在内容区打开；
- iframe 内的 `Ctrl+K` / `Ctrl+1..5` 也交给壳处理；
- 窄宽度下保留引擎页的移动标签栏，否则三栏内容会被移动媒体查询整体隐藏。

### 3.4 按需页面

- 「显示页面」把直播页交给原生 WebView2 子视图，矩形对齐舞台；
- 命令面板 / 确认框 / 设置等 HTML 浮层出现前先 `room_show(false)`，关闭后恢复；
- 返回信号画布只 `hide`，不 `close`，采集继续；
- 页面模式是原生子视图，因此没有圆角、阴影和裁剪，靠 1px 边框与留白形成画框。

### 3.5 后台采集

`browser` 采集模式（桌面默认）下：

1. 引擎里出现 `pending` 房间；
2. 壳的 `start_room_watcher` 建会话并把它置为 `starting`；
3. `browser_enqueue_tab_ex(..., show=false)` 创建一个**不可见**的采集控制台窗口；
4. 房间停止 / 删除时，watcher 向控制台发 `close`，关标签页并收尾；
5. 主窗口仍然只有一个，侧栏状态由 `/api/rooms` 轮询刷新。

采集控制台仍保留完整的浏览器标签页与地址栏，只在用户显式从托盘打开时显示。

---

## 4. 技术落点

### 4.1 本地壳 + 引擎 HTTP

- 壳页面 origin 是 `http://tauri.localhost`，可以直接调用 `room_*` / `live_*` / `diag_*`；
- 引擎页仍由 `127.0.0.1:<port>` 提供，壳通过 `fetch` 调 `/api/*`；
- 引擎 `server.py` 只对 `tauri.localhost` 放行 CORS，并处理 `OPTIONS` 预检；
- 主窗口初始化脚本注入 `window.__LN_ENGINE__` / `__LN_ENTRY__` / `__LN_DESKTOP__`；
- 主窗口启用 DWM 深色标题栏（`DWMWA_USE_IMMERSIVE_DARK_MODE`），避免蓝色系统标题栏和深色壳打架。

### 4.2 页面桥

`app/web/js/embed.js` 在 `?embed=1` 时：

- 给 `<html>` 加 `ln-embed` / `ln-embed-index` / `ln-embed-settings`；
- 隐藏 `.topbar` / `.top`，设置页隐藏背后的工作台与遮罩，抽屉铺满视口；
- 拦截同源 `<a href>`，`postMessage({type:'ln-navigate', href})`；
- 把 `Ctrl+K` / `Ctrl+1..5` 转成 `ln-key`；
- 非嵌入模式完全无副作用。

> 后续（docs/29）：桌面的观感/文案/右键菜单已迁到 `desktop/web`，`embed.js`
> 只保留协议；藏顶栏改由基座 `css/embed.css` 按 `data-ln-region` 锚点完成。

### 4.3 `window.open` 收口

- 同源引擎链接：主窗口 `on_new_window` 发 `shell_navigate`，壳在内容区打开；
- 外部链接：按设置页 `prefs.open_mode` 走应用内浏览器或系统浏览器；
- `deny` 新窗口，不再出现「应用内浏览器窗口」这个默认路径。

### 4.4 测试钩子

壳暴露 `window.__LN_SHELL__`（仅在页面里，不影响 UI）：

- `state()`、`view()`、`select()`、`wall()`、`palette()`、`page()`、`workbench()`、`live()`；
- 验收脚本用 `LIVENOTES_SPIKE=m1shell` 自动切换视图、打点、截图。

---

## 5. 验收

### 5.1 壳视图自动验收

```bash
python -I desktop/test/run_m1_spike.py m1shell
```

结果：`done=True missed=none`。截图：

| 状态 | 截图 |
|---|---|
| 收听 + 信号画布 | [m1shell-a-listen.png](prototypes/shots/m1shell-a-listen.png) |
| 监控墙 + 添加入口 | [m1shell-b-wall.png](prototypes/shots/m1shell-b-wall.png) |
| 命令面板 | [m1shell-c-palette.png](prototypes/shots/m1shell-c-palette.png) |
| 按需页面（原生子视图） | [m1shell-d-page.png](prototypes/shots/m1shell-d-page.png) |
| 工作台 | [m1shell-e-workbench.png](prototypes/shots/m1shell-e-workbench.png) |
| 文件中心 | [m1shell-f-files.png](prototypes/shots/m1shell-f-files.png) |
| 人物档案 | [m1shell-g-people.png](prototypes/shots/m1shell-g-people.png) |
| 设置 | [m1shell-h-settings.png](prototypes/shots/m1shell-h-settings.png) |

### 5.2 交互流程验收

```bash
python desktop/test/run_flow_check.py
```

通过 WebView2 远程调试口驱动真实页面，结果 `ok=True`：

| 动作 | 结果 |
|---|---|
| 添加直播间（`POST /api/rooms` + CORS） | 房间 5 → 6，toast「已加入收听」，自动选中新房间 |
| 命令面板 | 打开、10 项动作、方向键选中、Esc 关闭 |
| 重试异常房间 | 选中 `rmdemo04`，状态从 `failed` → `pending` |
| 单路停止 | 确认框标题正确，确认后状态「已停止」，在听数 2 → 1 |
| 停止全部 | 面板项命中，确认框显示「停止全部 4 路？」，Esc 可取消 |

### 5.3 后台采集冒烟

```bash
python -I desktop/test/run_capture_check.py
```

隔离数据里放一条 `pending` 房间，等 watcher 把它交给应用内浏览器，然后枚举该进程的可见顶层窗口：

```
room watcher: rmcapture01 -> desktop tab https://live.douyin.com/...
visible window: 'LiveNotes / 直播语记' 1456x939
handed=True visible=1 ok=True
```

说明采集控制台确实创建了，但没有可见窗口；主窗口仍是唯一可见顶层窗口。

---

## 6. 已知限制与未验证项

| 项 | 状态 |
|---|---|
| DPI 125% / 150% | 未复跑；本次验收在 100% 缩放下完成 |
| 焦点在原生直播页时的快捷键 | 未做 Tauri 全局快捷键；焦点进入子视图后壳收不到 `Ctrl+K` |
| 真实直播间的隐藏采集 | 只验证了「控制台窗口不可见 + 房间被接管」；真实抖音/快手页面在隐藏窗口里的长稳出块未压测 |
| 5 路内存 | 未在本轮重测；已知 16GB + WSL 4GB 的余量风险 |
| 关窗到托盘 | 代码默认拦截并最小化；未做本轮自动验收 |
| 工作台 / 文件 / 人物 / 设置视觉 | 仍是浅色旧页面，套在深色画框里；未整体深色化 |
| 标题栏 | 使用 Windows 原生深色标题栏；未做无边框自定义标题栏，非激活态标题文字偏暗 |
| 工作台内页跳转 | 依赖 `embed.js` 拦截；带 `target=_blank` 或 JS 直接 `location` 的入口未逐项覆盖 |
| 网页版 | 未套壳，仍保留原多标签行为 |
| macOS / Linux | 未验证 |

---

## 7. 文件清单

新增：

- `desktop/dist/shell.html` / `shell.css` / `shell.js`：单窗口壳；
- `app/web/js/embed.js`：嵌入模式适配；
- `desktop/test/run_m1_spike.py`：壳视图驱动 + 截图；
- `desktop/test/run_flow_check.py`：交互流程验收；
- `desktop/test/run_capture_check.py`：后台采集窗口冒烟；
- `desktop/test/win_shot.cs` / `win_shot.py` / `shot_window.ps1`：截图工具。

改动（与本主题相关）：

- `desktop/src-tauri/src/lib.rs`：壳主窗口、`m1shell` 驱动、`on_new_window` 收口；
- `desktop/src-tauri/src/rooms.rs`：隐藏采集控制台、watcher 的 `background` 参数；
- `app/server.py`：CORS 预检；
- `app/web/*.html`：引入 `embed.js`；
- `app/web/index.html` / `js/index/main.js` / `js/index/settings.js`：嵌入模式与设置直达；
- `desktop/src-tauri/capabilities/default.json`、`engine.rs`、`diag.rs`：窗口与诊断适配；
- `desktop/README.md`、`docs/23`、`docs/24`：文档同步。

---

## 8. 复现

```bash
# 1. 构建 debug 壳
cd desktop && cmd //c ".\\build.cmd build"

# 2. 壳视图 + 截图（写入 docs/prototypes/shots/）
python -I desktop/test/run_m1_spike.py m1shell

# 3. 交互流程（需要 websockets；不用 -I）
python desktop/test/run_flow_check.py

# 4. 后台采集窗口冒烟
python -I desktop/test/run_capture_check.py
```

三个脚本都会把 exe 复制到 `%LOCALAPPDATA%\Temp\livenotes-*\` 再启动，避免工作区 exe 无法写 `%LOCALAPPDATA%` 的沙箱问题；数据目录用 `LIVENOTES_HOME` / `LIVENOTES_DATA_DIR` 隔离，不碰真实配置。
