# 26 · UI 重构方案 B：Studio / Paper 落地说明

- **状态**：已实现（2026-11-11），工作区未提交
- **关联**：[docs/23 交互重构方案](23-desktop-ux-redesign.md)、[docs/25 单窗口工作区实现](25-desktop-workspace-implementation.md)、[原型与截图](prototypes/ui-directions.md)
- **范围**：桌面壳外观与引擎页嵌入换肤；交互模型沿用 docs/25，不改 Engine API

---

## 0. 结论先行

这一轮解决三件事：

1. **「引擎未连接」不是样式问题**：`tauri dev` 下壳页 origin 是 `http://127.0.0.1:1430`，被引擎 CORS 白名单挡掉，所有 `/api/*` 读不到；已改为放行任意回环 origin，并补了 Private Network Access 预检。
2. **一个窗口只有一套主题**：收听 / 实时用 studio（暖黑 + 琥珀），工作台 / 文件 / 人物 / 设置用 paper（暖纸 + 深琥珀）；切视图时壳与 iframe 同步换，不再出现深色壳包着冷白页面。
3. **去掉系统蓝色标题条**：主窗口与启动画面改为 `decorations(false)`，标题栏由壳页自绘（拖拽区 + 最小化 / 最大化 / 关闭），主题切换时整窗一致。

---

## 1. 「引擎未连接」根因与修复

### 现象

- 外部浏览器打开 `127.0.0.1:<port>/live` 能正常收听、飞书也能收到回复；
- 桌面壳左下角一直是「引擎未连接」，房间数 0；
- 引擎自身 `/api/rooms`、`/api/health` 均正常。

### 根因

`tauri dev` 构建的 exe 把 `devUrl = http://127.0.0.1:1430` 打进壳页，壳页 origin 因此是
`http://127.0.0.1:1430`；引擎 CORS 只允许 `http(s)://tauri.localhost`，于是：

- iframe 里的引擎页是**同源**加载（引擎自己的 origin），照常显示；
- 壳页用 `fetch` 调 `/api/*` 是**跨源**，被 CORS 拦掉，`online=false`。

所以「系统在跑、只有桌面端没反应」的表象成立，但原因在跨源策略，不是前端渲染。

### 修复（`app/server.py`）

```python
CORS_ORIGINS = {"http://tauri.localhost", "https://tauri.localhost"}
CORS_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "tauri.localhost"}

def _trusted_origin(origin: str) -> bool:
    ...  # 只信任 http(s) + 回环主机，任意端口
```

- `end_headers()` 对可信 origin 回 `Access-Control-Allow-Origin`；
- `do_OPTIONS()` 对不可信 origin 直接 403，可信时补
  `Access-Control-Allow-Private-Network: true`（Chromium 从公网页访问本机服务会先发 PNA 预检）；
- 引擎只监听回环地址，因此「回环 origin」就是全部可信来源，远程站点仍读不到响应。

### 验证

- `tests/test_cors.py`：13 条（放行回环各端口 / 拒绝远程 origin / PNA 预检），全过；
- `desktop/test/run_conn_probe.py` 隔离实例实测：`origin=http://127.0.0.1:1430`、`online=true`、`/api/health` 200；
- `run_ui_shots.py`：壳状态 `online:true, rooms:5, live:2`。

---

## 2. 三套方向与选型

原型：[docs/prototypes/ui-directions.html](prototypes/ui-directions.html)（`?d=a|b|c` 切换），说明见
[ui-directions.md](prototypes/ui-directions.md)。

| 方向 | 语言 | 结论 |
|---|---|---|
| A · Signal Desk | 全深色、青绿信号色、仪表盘感 | 长时间看字幕舒服，但文件 / 人物这类阅读页偏压抑，改动面最大 |
| **B · Studio / Paper** | 收听深色、资料暖纸，同一套琥珀强调 | **采用**：既保留收听时的专注感，又让阅读页回到纸面；主题按视图切换，改动集中在 token |
| C · Quiet Graphite | 全浅灰、低饱和、极简 | 最「现代」，但收听页失去舞台感，和信号画布的气质不搭 |

选 B 的关键理由：现有页面结构不动，只换 token 与少量硬编码表面，回退成本低；两套主题共用
`--amber` 强调色，切换视图时品牌感是连续的。

---

## 3. 方案 B 实现

### 3.1 无边框窗口与自绘标题栏

- `desktop/src-tauri/src/lib.rs`：主窗口与 `build_splash` 都加 `.decorations(false)`；
- `desktop/dist/shell.html`：`.topbar`、`.brand`、`.wordmark`、`.crumb` 标 `data-tauri-drag-region`；
  右上角新增 `.win-controls`（最小化 / 最大化 / 关闭到托盘）；
- `desktop/dist/shell.js`：`__TAURI__.window.getCurrentWindow()` 绑定三个按钮；
  非 Tauri 环境（纯浏览器调试）隐藏按钮；标题栏双击（非按钮 / 输入框）切换最大化；
- `desktop/src-tauri/capabilities/default.json`：`core:window:default` 只放行只读查询和
  `internal-toggle-maximize`，最小化 / 最大化 / 关闭必须显式加
  `core:window:allow-minimize`、`allow-toggle-maximize`、`allow-close`，否则 IPC 被 ACL
  拒绝且按钮静默无反应（和 M1 spike 里远程 IPC 被拒是同一类问题）。

这样 Windows 的蓝色系统标题条不再压在深色或纸面主题上，窗口圆角与阴影由系统合成器保留。

### 3.2 整窗双主题

```js
const VIEW_THEME = {
  listen: 'studio', live: 'studio',
  workbench: 'paper', files: 'paper', people: 'paper', settings: 'paper', frame: 'paper',
};
```

- `switchView()` 里同步 `document.documentElement.dataset.theme`；
- iframe URL 由 `withTheme()` 追加 `theme=paper|studio`；
- `app/web/js/embed.js` 读到 `theme` 后给 `<html>` 设 `data-theme`，paper 时注入
  `/static/css/paper.css`；网页版（无参数）完全不变。

> 后续（docs/29）：`paper.css` 已从 `app/web/css/` 迁到 `desktop/web/css/`，
> 由 `/api/host/manifest` 的 css 清单加载；`embed.js` 不再认识 paper，只落
> `data-theme`。主题名仍是协议的一部分，取值由皮肤 CSS 决定。

### 3.3 暖纸 token 与页面覆盖

- `desktop/dist/shell.css`：新增 `html[data-theme="paper"]` 变量块（`--bg #F3F0E9`、
  `--panel #FBF9F4`、`--ink #23201B`、`--amber #A76A22` 等），并覆盖顶栏 / 活动栏 /
  状态栏 / 命令面板 / 弹层 / 滚动条；
- `app/web/css/paper.css`：覆盖引擎页两套变量命名（`--ink/--text`、`--border/--line`、
  `--accent/--accent-soft`…），以及写死白底的面板、表格、输入框；
- 页面里少数写死的浅蓝（人物页主按钮、选中行、标签、高光榜筛选、阅读页脏稿提示）在纸面下
  统一换成琥珀底，避免暖色页面里跳出冷蓝；
- 焦点环、链接、选区同步换琥珀，滚动条改暖灰。

### 3.4 主题感知的数据色

`app/web/js/index/core.js` 按 `data-theme` 选两套说话人 / 分类色：

- studio：原来的蓝 / 绿 / 紫高饱和色；
- paper：琥珀 / 墨绿 / 赭石 / 绛红等低饱和暖色，和时间轴、标签底色同一族。

### 3.5 过渡

壳的顶栏、活动栏、侧栏、检查器、状态栏、内容头加 `background-color / border-color / color`
的 160ms 过渡；iframe 因跨文档无法与壳做同一段动画，靠同色系取值让切换不刺眼。

---

## 4. 验收

| 项 | 结果 |
|---|---|
| 引擎连接 | 隔离实例 `online=true`，5 个直播间 / 2 路在听，状态栏显示实际端口 |
| 主题同步 | 切工作台 / 文件 / 人物后 `data-theme=paper`，iframe URL 均带 `theme=paper` |
| 标题栏 | 无系统蓝条；窗口按钮由壳页绘制，`__TAURI__.window.getCurrentWindow` 可用 |
| 窗口按钮 | Win32 实测：最大化 `IsZoomed=true`（-8,-8,1928,1048）、还原回原矩形、最小化 `IsIconic=true`、关闭进程退出 0 |
| 截图 | `docs/prototypes/shots/impl-{listen,workbench,files,people}.png` |
| 单测 | `python -m pytest tests/ -q` → 462 passed |

---

## 5. 已知限制

- **拖拽移动 / 边缘缩放 / 双击最大化只做了代码路径验证**，未在 125% / 150% DPI 下人工试；
- 原生直播页子视图（`window.add_child`）不参与 HTML 换肤，页面模式仍是引擎页原色；
- iframe 与壳是两套文档，主题切换没有跨文档动画，靠配色统一；
- 关闭按钮走「关闭到托盘」，与系统关闭行为一致，但无边框后没有系统菜单（右键标题栏不再出现）。

---

## 6. 文件清单

| 文件 | 改动 |
|---|---|
| `app/server.py` | CORS 回环白名单 + PNA 预检 |
| `tests/test_cors.py` | 新增 13 条 CORS 用例 |
| `desktop/src-tauri/src/lib.rs` | 主窗口 / 启动画面 `decorations(false)` |
| `desktop/src-tauri/capabilities/default.json` | 显式放行最小化 / 最大化 / 关闭三个窗口命令 |
| `desktop/dist/shell.html` | 拖拽区、窗口按钮、`data-theme` |
| `desktop/dist/shell.css` | paper token 块、窗口按钮、过渡 |
| `desktop/dist/shell.js` | 视图主题映射、iframe 传参、窗口控制 |
| `app/web/js/embed.js` | 读 `theme`、注入 `paper.css` |
| `app/web/css/paper.css` | 新增，引擎页纸面覆盖 |
| `app/web/js/index/core.js` | 主题感知的说话人 / 分类色 |
| `desktop/test/run_conn_probe.py` | 新增，CORS / 连接探针 |
| `desktop/test/run_ui_shots.py` | 新增，四屏截图验收 |
| `desktop/test/run_win_controls.py` | 新增，窗口按钮 Win32 验收 |

---

## 7. 复现

```bash
python desktop/test/run_conn_probe.py     # 连接与 CORS
python desktop/test/run_ui_shots.py       # 四屏截图 → docs/prototypes/shots/
python desktop/test/run_win_controls.py   # 自绘标题栏按钮
python -m pytest tests/ -q                # 单测
```

> 已打开的旧桌面实例不会热更新：需从托盘退出后重新 `run-dev.cmd`，才会加载新的壳与
> Python CORS 修复。
