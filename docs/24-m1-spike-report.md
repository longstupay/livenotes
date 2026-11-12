# M1 单窗口壳 · Spike 报告

- **日期**：2026-11-11
- **范围**：只验证 [docs/23](23-desktop-ux-redesign.md) 第 7 节里挡在 M1 前面的三个未知数——主窗口 IPC、原生子视图挂主窗口、z 序；不实现正式 M1 代码。
- **后续**：M1–M4 第一版已按本报告结论落地，见 [docs/25](25-desktop-workspace-implementation.md)。
- **环境**：Windows 10 19045 / 1920×1080 / 缩放 100% / Tauri 2.12.5 / WebView2 / debug 构建。
- **驱动**：`LIVENOTES_SPIKE=m1`（远程主窗口）与 `m1local`（本地壳 + iframe）；脚本 [desktop/test/run_m1_spike.py](../desktop/test/run_m1_spike.py)，测试页 [spike-stage.html](../app/web/spike-stage.html) 和 [spike-shell.html](../desktop/dist/spike-shell.html)。

---

## 结论先行

| # | 问题 | 结果 | 判定 |
|---|---|---|---|
| 1 | 主窗口加载 `http://127.0.0.1` 时，网页能调壳的自定义命令吗？ | 注入成功，但调用被 ACL 拒绝：`spike_report not allowed. Plugin not found` | ❌ 路线 a 的简单版不成立 |
| 2 | 主窗口改成本地页（`tauri.localhost`）呢？ | `spike_report`、`room_open`、`room_show` 全部成功 | ✅ 路线 b 成立 |
| 3 | `add_child` 挂主窗口能定位/缩放/隐藏/显示/关闭吗？ | 760×520、520×360 两次布局都精确；hide/show/close 正常 | ✅ 成立 |
| 4 | 原生子视图和 HTML 浮层的 z 序？ | 原生视图永远在最上层；浮层全屏压暗时子视图矩形保持原亮度，中央卡片被完全遮住；`hide()` 后浮层完整可见 | ✅ 规则确认 |

**建议**：M1 采用 **本地壳页（路线 b）+ iframe 复用现有引擎页**，不走远程 origin 的 app 命令 ACL。路线 a 不是“加一行 `remote.urls`”就能用；它需要为每个壳命令生成 app ACL 权限，并把权限按 webview 收窄，成本与风险都高于本地壳。

---

## 1. 主窗口 IPC：远程页为什么失败

主窗口仍是 `WebviewUrl::External(http://127.0.0.1:<port>/)`，实验时临时在 capability 里加了（收尾已回滚）：

```json
"remote": { "urls": ["http://127.0.0.1:*", "http://localhost:*"] }
```

页面里探测结果（写在 URL hash 里，由 Rust 读回）：

```json
{
  "tauri": "object",
  "internals": "object",
  "tauriInvoke": "err:spike_report not allowed. Plugin not found",
  "internalsInvoke": "err:spike_report not allowed. Plugin not found"
}
```

说明三件事：

1. `withGlobalTauri` 和 IPC 初始化脚本**确实注入**到了 loopback 页面（`__TAURI__` / `__TAURI_INTERNALS__` 都是 object）。
2. `remote.urls` 只解决了“这个 origin 允许走 IPC”，没有解决“这个自定义命令允许被调用”。
3. Tauri 2.12.5 对远程 origin 的 app 命令要求显式 ACL：

   `tauri/src/webview/mod.rs` 的判定是
   `plugin_command.is_some() || has_app_acl_manifest || !is_local`。
   远程页最后一项为真，于是必须 `invoke.acl.is_some()`；而项目没有 `AppManifest`，`__app-acl__` 清单不存在，所以命令查不到，报 `Plugin not found`。

要让路线 a 成立，需要：

- 在 `build.rs` 用 `tauri_build::AppManifest::new().commands(&[...])` 列出**所有**壳命令（`room_*`、`live_*`、`diag_*`），生成 `allow-*`；
- capability 把这些 `allow-*` 放进 `permissions`，并用 `webviews: ["main"]` 把权限收窄到主 webview，避免同窗口里的抖音子视图也拿到命令；
- 以后每加一个命令，都要同步 build.rs + capability，漏一个就是运行时 `not allowed`。

这不是不可行，但它把“M1 先跑起来”变成了“先重做一套 ACL”。**本 spike 不建议在 M1 走这条路。**

## 2. 本地壳：M1 的实际路线

`LIVENOTES_SPIKE=m1local` 时，主窗口改为：

```rust
WebviewUrl::App("spike-shell.html".into())
// init script: window.__LN_ENGINE__ = "http://127.0.0.1:<port>/"
```

本地壳页（`http://tauri.localhost`）加载 56px 顶栏 + 引擎页 iframe，然后直接：

```js
await invoke("room_open", {
  label: "m1-stage",
  url: engine + "/static/spike-stage.html",
  sid: "", capture: false,
  x: 380, y: 180, w: 640, h: 420
});
```

日志：

```
spike-report tag=m1local-ipc detail={"origin":"http://tauri.localhost","engine":"http://127.0.0.1:60319/"}
spike-report tag=m1local-iframe-load detail={"engine":"http://127.0.0.1:60319/"}
spike-report tag=m1local-child-open detail={"ok":true}
spike-report tag=m1local-child-hidden detail={"ok":true}
spike-report tag=m1local-child-shown detail={"ok":true}
```

本地 origin 的 app 命令不经过远程 ACL（项目没有 app manifest），因此现有 `room_*` / `live_*` / `diag_*` 全部可用，不需要改 capability。引擎页在 iframe 里正常加载出完整工作台。

**M1 壳的结构**：

```
main window (tauri.localhost, 本地 dist/shell.html)
├── 壳 chrome：活动栏 / 对象列表 / 检查器 / 状态栏（本地 HTML，可调 IPC）
├── 内容区：iframe(engine)  ← 现有工程页先原样复用
└── 原生 WebView2 子视图（直播舞台/页面模式）
    └── 由壳调 room_open/room_layout/room_show 定位到内容区矩形
```

跨 origin 的 iframe 不需要读写 DOM；只需要一条 `postMessage` 桥：iframe 里的页面要“开直播间/切对象”时发给壳，壳再调 IPC。现有 `window.open` 仍由主窗口的 `on_new_window` 接住，M1 要把它从“打开应用内浏览器窗口”改成“通知壳在内容区打开”。

## 3. 子视图挂主窗口

| 操作 | 请求 | 子视图自报 | 结果 |
|---|---|---|---|
| 打开 | x=360, y=140, w=760, h=520 | 760×520 @1x | ✅ 位置/尺寸精确 |
| 移动+缩放 | x=820, y=300, w=520, h=360 | 520×360 @1x | ✅ |
| 隐藏 | `room_show(false)` | — | ✅ HTML 浮层完整露出 |
| 显示 | `room_show(true)` | — | ✅ 重新盖住浮层 |
| 关闭 | `room_close` | — | ✅ 无残留 |

证据：

- [打开 760×520](prototypes/shots/m1-a-child-visible.png)
- [隐藏后浮层](prototypes/shots/m1-b-child-hidden.png)
- [移动+缩放到 520×360](prototypes/shots/m1-c-child-moved.png)
- [关闭后](prototypes/shots/m1-d-child-closed.png)
- [本地壳 + iframe + 子视图](prototypes/shots/m1local-a-shell-child.png)
- [本地壳隐藏子视图](prototypes/shots/m1local-b-hidden.png)
- [本地壳重新显示](prototypes/shots/m1local-c-shown.png)

子视图里 `fetch('/api/health')` 返回 OK，说明子 WebView2 和主窗口共享同一个 loopback 引擎，不需要额外代理。

## 4. z 序与浮层规则

测试方式：在主窗口注入一个 `position:fixed; inset:0; z-index:2147483647` 的压暗层，中央放一张“命令面板”卡片，卡片正好落在子视图矩形里。

结果：

- 子视图可见时：压暗层把子视图以外的区域压暗，**子视图矩形保持原亮度**，中央卡片完全看不见 → 原生视图在 HTML 之上，且不受 z-index 影响。
- `room_show(false)` 后：压暗层和卡片完整可见。
- `room_show(true)` 后：子视图再次盖住卡片。

因此方案 B 的技术规则不是“建议”而是硬约束：

1. 命令面板、设置抽屉、停止确认、toast 等任何需要盖住舞台的 HTML，打开前必须 `room_show(false)`，关闭后恢复；否则浮层会被原生视图“挖洞”。
2. 不要给舞台做圆角、阴影、裁剪；原生子视图是矩形，不能参与 CSS 布局。
3. 分屏/拖拽分隔条时，先 hide 再改布局，松手后 show；或直接改 `room_layout`。本次只验证了最终矩形精确，闪烁与焦点丢失没有录屏验证。
4. 全屏压暗层要处理滚动条：本次截图右侧约 20px 的页面滚动条区域没有被 `inset:0` 覆盖（引擎页有纵向滚动条），正式壳需要在浮层打开时锁 `body` 滚动或把浮层宽度设为 `100vw`。

## 5. 还没验证的

这次只回答了 M1 前三个问题。M1–M4 第一版落地后，部分开口项已有结论（详见 [docs/25](25-desktop-workspace-implementation.md)）：

- ✅ **iframe 内的交互**：`embed=1` 隐藏顶栏、同源链接与 `Ctrl+K` / `Ctrl+1..5` 已通过 `postMessage` 交回壳；添加 / 重试 / 停止 / 命令面板已用 CDP 驱动验收。
- ✅ **后台采集宿主**：`browser` 模式下待启动房间由**不可见的**采集控制台接管，进程枚举只有一个可见主窗口。
- ⏳ **DPI 125%/150%**：仍未复跑；本次实现验收在 100% 缩放下完成。
- ⏳ **焦点与全局快捷键**：仍未做 Tauri 全局快捷键；焦点进入原生直播页后壳收不到键盘事件。
- ⏳ **真实直播间的隐藏采集长稳**：只验证了控制台不可见与房间被接管，真实页面在隐藏窗口里的持续出块未压测。
- ⏳ **5 路内存**：未在本轮重测；16GB + WSL 4GB 的余量风险仍在。
- ⏳ **窗口关闭到托盘**：已实现默认拦截，但未做本轮自动验收。

## 6. 对 docs/23 的修正

- 第 7 节第 1 条：路线 a 的“加 `remote.urls`”**不足以**放行自定义命令；需要 app ACL。M1 直接走路线 b。
- 第 7 节第 2、3 条：在 100% 缩放下已验证通过；高 DPI 和焦点/快捷键仍待验证。
- 第 7 节第 5 条（iframe）：引擎页能在 iframe 里加载；交互细节待 M1 首周验证。
- 方案 B 的“主窗口加载引擎页”改为“主窗口加载本地壳页，引擎页在内容区 iframe”；这也是方案 A 的壳，只是内容区从标签页换成单舞台。

## 7. 复现

```bash
# 构建（VS 2022 环境 + 固定工具链）
desktop/build.cmd build

# 远程主窗口：预期 IPC 被 ACL 拒绝，子视图/z 序通过
python -I desktop/test/run_m1_spike.py m1

# 本地壳 + iframe：预期全部通过
python -I desktop/test/run_m1_spike.py m1local
```

脚本会把 exe 复制到 `%LOCALAPPDATA%\Temp\livenotes-m1\` 再启动——工作区里的 exe 被沙箱禁止写 `%LOCALAPPDATA%`，直接跑会在 splash 阶段报 `0x800700AA`。截图写入 `docs/prototypes/shots/`，应用日志追加到 `desktop/src-tauri/boot.log`。
