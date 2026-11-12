# 21 · 桌面故障诊断：托盘提示、日志打包与一键导出

- **状态**：已实现（2026-11-10）
- **范围**：Tauri 壳的失败检测与取证；不改 Python 引擎的采集/ASR 逻辑
- **前置**：[docs/20](20-tauri-2-adr.md) 的 Tauri 2 壳（D1–D5）

---

## 1. 问题

桌面用户没有「打开控制台」这条路。引擎崩了、房间视图崩了、直播断流了，
如果界面只是黑着/卡着，用户能提供的信息只有一句「不能用了」。

D6 的目标是让这三类失败都能被壳看见，并且用户能在一个地方：

1. 知道**哪一类**失败、影响了几路；
2. 拿到**可提交的现场**（日志、事件、引擎快照、WebView2 dump）；
3. 不需要命令行，托盘右键即可完成。

D6 **不负责自动重启引擎**。先把「为什么坏」留下来，重启策略另做。

---

## 2. 三类失败怎么发现

| 失败 | 检测方式 | 事件 `kind` | 托盘 |
|---|---|---|---|
| 引擎退出 | `Child::try_wait()` 看到进程结束 | `engine_exited` | `✖ 引擎已退出 · 打开诊断` |
| 引擎假死 | `/api/health` 连续 3 次失败 | `engine_unreachable` | `⚠ 引擎无响应 · 打开诊断` |
| 房间视图崩溃 | WebView2 `ProcessFailed` 事件 | `room_process_failed` | `⚠ 房间视图异常 · 打开诊断` |
| 断流 | 轮询 `/api/diagnostics` 的 `live[].idle_s` | `stream_stalled` / `stream_recovered` | `⚠ N 路断流 · 打开诊断` |

补充规则：

- 壳正常退出时先 `mark_shutdown()` 再停引擎，否则回收动作会被记成
  `engine_exited`。`RunEvent::ExitRequested` 和 `Exit` 都做了这件事。
- 房间视图崩溃只对「非主动关闭」计数。`room_close` 会先写一个 10 秒的
  closing 标记，标记期内的 `ProcessFailed` 不记事件、不重载。
- 同一房间 10 分钟内最多自动重载 3 次。超过后仍记录崩溃，但提示
  「需要重新打开这一路」，避免无限重载循环。
- 断流阈值优先用引擎返回的 `config.stall_warn_s`（默认 30 秒）；
  测试时可用 `LIVENOTES_SHELL_STALL_SECONDS` 钉死。恢复判定是
  `idle < max(threshold/2, 5s)`，避免阈值边缘反复告警。
- 场次从引擎列表消失连续 2 次轮询后，壳删除本地断流记录并发
  `stream_ended`，托盘不再长期挂着旧场次。

引擎 watcher 和断流 watcher 都是壳里的后台线程，轮询间隔默认 3 秒，
`LIVENOTES_SHELL_POLL_MS` 可调（500–60000ms）。

---

## 3. 托盘

托盘菜单第一项就是状态，点击打开诊断页；tooltip 同步状态；失败时图标右上角
叠加红色感叹号（`warning_icon()` 在 32×32 图标上绘制，无需额外资源）。

| 状态 | 菜单文字 |
|---|---|
| 启动中 | `… 正在启动引擎` |
| 正常 | `✓ 运行正常 · 打开诊断` |
| 断流 | `⚠ N 路断流 · 打开诊断` |
| 房间视图异常（10 分钟内） | `⚠ 房间视图异常 · 打开诊断` |
| 引擎无响应 | `⚠ 引擎无响应 · 打开诊断` |
| 引擎退出 | `✖ 引擎已退出 · 打开诊断` |

托盘在引擎启动**之前**创建。引擎起不来时错误页和托盘仍然可用，这是用户
导出诊断包的唯一入口。错误页 `error.html` 也直接提供「导出诊断包 / 打开日志目录 /
打开诊断页」三个按钮。

---

## 4. 日志与事件

数据根默认 `%LOCALAPPDATA%\LiveNotes`，可用 `LIVENOTES_HOME` 整体改到别处。

| 文件 | 内容 | 轮转 |
|---|---|---|
| `logs/shell.log` | 壳的人读日志，含 `diag level=... kind=...` 单行摘要 | 不轮转 |
| `logs/diag.jsonl` | 机器可读诊断事件（时间、级别、kind、message、detail） | 2MB，转成 `.jsonl.1` |
| `logs/engine.log` | 引擎 stdout/stderr（原有） | 引擎自己管 |
| `data/logs/app-*.jsonl` | 引擎结构化日志（原有） | 引擎自己管 |

诊断事件在内存里保留最近 240 条，导出和诊断页都取这个环形缓冲。
`diag.jsonl` 里的每条事件与内存事件同源，壳崩溃后仍可从文件取证。

---

## 5. 一键导出诊断包

入口：

- 托盘 →「导出诊断包…」
- 诊断页 `diagnostics.html` →「导出诊断包」
- 错误页 `error.html` →「导出诊断包」

文件写到桌面（OneDrive 桌面也识别），可用 `LIVENOTES_DIAG_DIR` 指定目录。
命名 `LiveNotes-diagnostics-YYYYMMDD-HHMMSSZ.zip`，导出成功后自动在资源管理器定位。

包内容：

```
README.txt                 包说明与排查顺序
summary.json               壳/引擎状态、失败事件、路径、脱敏环境变量
diag/events.json           最近诊断事件
snapshots/health.json      /api/health 快照
snapshots/diagnostics.json /api/diagnostics 快照（房间 URL 已脱敏）
logs/shell.log             壳日志尾部（单文件最多 8MB）
logs/engine.log            引擎日志尾部（单文件最多 8MB）
logs/diag.jsonl            诊断事件文件
logs/app-*.jsonl           引擎结构化日志，最近 3 个
crash-dumps/*.dmp          WebView2 崩溃转储，最多 5 个、每个最多 8MB
```

**引擎已经死了也能导出**：事件和日志来自文件，HTTP 快照失败时写成
`{"error": "..."}`，不会因为拿不到 `/api/health` 而中止打包。

### 隐私边界

不包含：

- `settings.json`、LLM Key、飞书配置；
- 录音、字幕正文；
- `/api/metrics`（其中的 `last_line` 是字幕片段）；
- `/api/live/list`（同样带 `last_line`）和 `/api/rooms`（带未脱敏的原始直播间 URL）；
  在跑场次与房间状态改从脱敏后的 `/api/diagnostics` 取；
- 直播间 URL 的 query 参数（只保留 host+path）。

环境变量只导出 `LIVENOTES_*`，名字含 `KEY/TOKEN/SECRET/PASSWORD/COOKIE`
的值替换为 `***`。`summary.json` 的 `privacy` 字段写明了这条边界。

---

## 6. 测试开关

| 变量 | 作用 |
|---|---|
| `LIVENOTES_DIAG_DIR` | 诊断包输出目录（默认桌面） |
| `LIVENOTES_DIAG_AUTO_EXPORT=1` | 失败时自动导出，最多 3 次、间隔至少 5 秒；仅供自动化 |
| `LIVENOTES_SHELL_STALL_SECONDS` | 钉死壳的断流阈值，不再跟随引擎配置 |
| `LIVENOTES_SHELL_POLL_MS` | 壳轮询 `/api/diagnostics` 的间隔，默认 3000 |
| `LIVENOTES_SPIKE=d3crash` | 自动开房间 A，18 秒后 CDP `Page.crash`，再关掉 A |

---

## 7. 验收

```bash
python desktop/test/verify_diag.py
```

（自定义 `CARGO_TARGET_DIR` 时用 `--shell <exe>` 或 `LIVENOTES_SHELL_EXE` 指定壳。）

脚本从**外部**取证，不采信壳自报的成功：壳日志、`logs/diag.jsonl`、
引擎 HTTP、导出的 zip 四路交叉。步骤：

1. 起测试站和壳，确认 `/api/diagnostics` 有正在出块的场次；
2. CDP `Page.crash` 真崩渲染进程 → 等 `room_process_failed` 和自动重载日志；
3. 关掉房间 A（引擎会话还在）→ 等关闭之后的 `stream_stalled`；
4. 校验包含崩溃与断流的诊断包结构、summary、隐私；
5. `taskkill` 引擎 → 等 `engine_exited`，校验退出后的诊断包仍能生成、
   `snapshots/health.json` 记录连接失败。

2026-11-10 实测通过，覆盖 `render_process_exited` 自动重载、8 秒断流、
引擎退出后导出包（含 `crash-dumps/*.dmp`）。自动导出有 5 秒节流，
验收脚本在杀引擎前会等过节流窗口，保证 `engine_exited` 也能落一个包。

已知边界：

- `Page.crash` 依赖 WebView2 的 CDP，属于测试路径，只在
  `LIVENOTES_SPIKE=d3crash` 下启用；
- 测试站是循环播放的短片，句间有自然停顿，所以验收把壳阈值设为 8 秒；
  生产默认 30 秒，并由引擎的 `stall_warn_s` 决定；
- 目前只记录失败，不自动重启引擎。
