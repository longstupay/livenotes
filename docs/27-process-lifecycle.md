# 进程生命周期与关窗语义（P0–P2）

2026-11-11。解决的问题：关掉桌面端后，任务管理器里还留着 `python.exe`
（引擎、飞书机器人、采集代理）和 Chrome/ffmpeg 子进程；再启动又会出现两套
引擎共用同一个库、飞书重复回复。

根因不是「退出没写」，而是四条路径各漏一段：

| 路径 | 以前的行为 | 漏在哪 |
|---|---|---|
| 点 X | 窗口隐藏到托盘，进程全部继续跑 | 没有提示，用户以为已经退出 |
| 任务管理器结束壳 | 引擎/helper 没有父进程看门狗，继续跑 | 没有 OS 级进程树回收 |
| 壳崩溃 / 关终端 | 同上；`tauri dev` 的 node 也留下 | 同上 + 开发态没包装 |
| 再启动 | 新引擎抢同一数据目录，旧引擎还在 | 没有单实例锁与接管 |

对应四层：

1. **语义层**：关窗 ≠ 退出；有录音先问，没有录音按设置。
2. **进程层**：Job Object + 父进程看门狗，壳怎么死都不留孤儿。
3. **单实例与残留自愈**：`engine.lock` + `engine.json` + 健康校验 + 接管。
4. **开发态**：`run-dev.ps1` 收掉 `tauri dev` 整棵进程树。

## 1. 语义层：关窗与退出

设置页「桌面版 → 关闭窗口时」（`prefs.close_action`）：

| 值 | 行为 |
|---|---|
| `tray`（默认） | 隐藏到托盘，录音继续；首次会 toast 说明 |
| `quit` | 直接退出程序 |

有房间在录音（`pending` / `starting` / `live`）时，一律先弹三选：

- **取消**：什么都不做；
- **最小化到托盘**：隐藏窗口，继续录音；
- **停止并退出**：对每路 `POST /api/rooms/close`，再 `shell_quit`。

托盘菜单：

- 「显示主窗口」：`show + unminimize + set_focus`；
- 「停止全部录音」：发 `shell_stop_all`，壳页走已有的停止全部流程；
- 「退出 LiveNotes」：有录音时发 `shell_quit_requested`，壳页弹同样的三选；
  没有录音直接 `app.exit(0)`。

实现要点：`CloseRequested` 一律 `prevent_close`，发 `shell_close_requested`
给壳页；壳页先回执 `shell_close_ack`，再决定隐藏/退出。2 秒没有回执
（壳页加载失败/卡死）由 Rust 兜底隐藏，不会出现「点了 X 没反应」。

截图：`docs/prototypes/shots/close-modal.png`。

## 2. 进程层：Job Object + 看门狗

### Windows Job Object

壳在 `spawn` 引擎前创建 Job，设置
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，再把引擎进程挂进去。引擎拉起的
飞书机器人、采集代理、Chrome、ffmpeg 默认继承 Job。壳进程无论怎么消失
（崩溃、任务管理器、关终端），内核关闭最后一个 Job 句柄时会杀掉整棵树。

接管的引擎（`engine.json` 里的 pid）也会用
`OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE)` + `AssignProcessToJobObject`
挂进新的 Job。

### 父进程看门狗

双保险，覆盖「Job 创建/挂载失败」和「手动起的引擎」：

- 壳通过 `LIVENOTES_PARENT_PID` 把 pid 传给引擎；
- `app/runtime.py::watch_parent` 每 2 秒检查一次父进程；
- 引擎死亡 → `srv.shutdown()`，走 `finally` 正常收尾（停 helper、停采集、关库）；
- helper（`run.py --feishu-bot / --capture-agent`）也挂同一个看门狗，
  父进程没了直接 `os._exit(0)`。

`pid_alive` 在 Windows 上区分 `ERROR_INVALID_PARAMETER(87)`（PID 已不存在）
和 `ERROR_ACCESS_DENIED(5)`（进程还在、权限更高）：前者判定死亡，后者保守
认为存活，避免权限问题导致误杀。

## 3. 单实例与残留自愈

数据目录下新增：

```
data/runtime/engine.lock    # 独占锁（msvcrt.locking / fcntl.flock）
data/runtime/engine.json    # {pid, port, host, started_at, data_dir, version}
```

启动顺序：

1. `run.py` 先 `runtime.acquire(port, host)`；
2. 抢锁成功 → 写 `engine.json`，继续启动；
3. 抢锁失败 → 读 `engine.json`，`GET /api/health` 探活：
   - 健康且 `health.pid == engine.json.pid` → 本进程退出码 **3**；
   - 不健康 → 确认 pid 是本应用进程（`python.exe` / `pythonw.exe` /
     `engine.exe`）后 `taskkill /T /F`，重试一次；
4. 壳的 `Engine::acquire` 先读 `engine.json` 并校验健康，健康就直接接管
   （不再 spawn）；spawn 的引擎返回 3 时，最多重试接管 10 × 200ms；
5. 退出时 `runtime.release()` 删除 `engine.json` 并解锁；壳的
   `cleanup_runtime` 只在 pid 匹配时删，避免误删别的实例的记录。

`/api/health` 现在带 `pid / port / started_at`，这是「端口上跑的是不是
engine.json 里那个进程」的唯一依据，避免端口被别的程序复用后误接管。

诊断页新增「结束残留引擎」按钮（`diag_engine_kill`）：只接受
`engine.json` 里的 pid，拒绝当前正在使用的引擎，且要求进程名是
`python.exe` / `engine.exe` 才执行。

## 4. 开发态

`desktop/run-dev.cmd` 改为调用 `run-dev.ps1`：用 `Start-Process -PassThru`
起 `npx tauri dev`，`finally` 里 `taskkill /PID <pid> /T /F`，Ctrl+C 或
终端关闭时不再留下 node / cargo 开发服务器（1430 端口）。

## 代码位置

| 文件 | 职责 |
|---|---|
| `app/runtime.py` | 锁、engine.json、健康探活、残留回收、父进程看门狗 |
| `run.py` | 启动时 acquire/release；引擎与 helper 的看门狗 |
| `app/server.py` | `/api/health` 带 pid/port/started_at |
| `app/settings.py` | `prefs.close_action`（tray/quit） |
| `desktop/src-tauri/src/engine.rs` | Job Object、接管、退出回收、stop 幂等 |
| `desktop/src-tauri/src/lib.rs` | 关窗事件、回执、shell_hide/shell_quit、托盘退出 |
| `desktop/src-tauri/src/diag.rs` | engine pid/started_at、diag_engine_kill |
| `desktop/dist/shell.js` | 三选弹窗、设置读取、停止并退出 |
| `desktop/dist/diagnostics.html` | 「结束残留引擎」按钮 |
| `desktop/run-dev.ps1` | 开发态进程树回收 |

## 验收

```bash
python -m pytest tests/test_runtime.py tests/test_settings.py -q
python desktop/test/run_exit_check.py       # 正常退出：引擎/helper 零残留
python desktop/test/run_hardkill_check.py   # 硬杀壳：Job Object 收掉整棵树
python desktop/test/run_adopt_check.py      # 手动引擎被接管，退出时一起收掉
python desktop/test/run_close_check.py      # 三选弹窗 / 托盘 / 退出设置
```

2026-11-11 本机结果：

- `python -m pytest tests/ -q`：470 passed；
- `run_exit_check.py`：`app exited code=0`，`survivors: none`；
- `run_hardkill_check.py`：硬杀壳后 `survivors: none`（引擎 + capture helper）；
- `run_adopt_check.py`：接管后 `engine.json pid` 不变、只有一个 `run.py`，
  壳退出后 `engines after shell exit: none`；
- `run_close_check.py`：有录音时弹窗三个按钮、选托盘后主窗口
  `IsWindowVisible=false` 且进程存活、选「停止并退出」后房间 `stopped`、
  无残留；`close_action=quit` 时点关闭直接退出。

## 已知限制

- **旧版本留下的高权限进程**：如果残留进程是以管理员权限启动的，普通权限的
  壳无法结束它。诊断页会提示失败；需要以管理员身份运行一次
  `taskkill /PID <pid> /T /F`。新版本正常启动的进程不会出现这种情况。
- **托盘菜单的三选弹窗**需要人工点一次确认（自动化脚本验证了壳页弹窗路径）；
- 单实例锁按「数据目录」隔离：`LIVENOTES_ALLOW_MULTI=1` 的调试实例会接管
  同一个引擎，而不是另起一套——这正是期望行为。
