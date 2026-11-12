# -*- coding: utf-8 -*-
"""一键启动：初始化数据库 -> 起本地服务 -> 打开浏览器。

    python run.py            # 普通启动（只监听 127.0.0.1，手机连不上）
    python run.py --lan      # 同时监听局域网，手机才能点开推送里的链接
    python run.py --reload   # 开发模式：改 app/*.py 自动重启（见 tools/dev.py）
    # 端口/监听地址可用 LIVENOTES_PORT / LIVENOTES_HOST 覆盖
    # LIVENOTES_NO_BROWSER=1 可禁止自动开浏览器（守护进程重启时用它）
    # LIVENOTES_SEED_DEMO=1 空库时生成演示场（默认不生成）
    # LIVENOTES_HELPERS=1 时顺带拉起飞书机器人和采集代理（桌面壳用）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# 打包态：PyInstaller 把数据和 tools/ 放在 _internal（sys._MEIPASS）里。
BUNDLE = Path(getattr(sys, "_MEIPASS", ROOT))
TOOLS = BUNDLE / "tools"


def _run_tool(name: str) -> int:
    """engine.exe --feishu-bot / --capture-agent：用同一个 exe 跑 tools/ 里的脚本。

    打包态只有一个入口，飞书机器人和采集代理靠这个分支复用 engine.exe；
    开发态也走这里（见 _helper_cmd），这样 helper 也能统一挂父进程看门狗。
    """
    import runpy
    path = TOOLS / f"{name}.py"
    if not path.is_file():
        print(f"[helpers] 找不到 {path}", flush=True)
        return 2
    flags = {"--feishu-bot", "--capture-agent"}
    sys.argv = [str(path)] + [a for a in sys.argv[1:] if a not in flags]
    # capture_agent 在 direct 模式下 `from direct_capture import ...`，
    # 打包态没有脚本目录在 sys.path 里，这里补上。
    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    try:
        from app import runtime
        parent = runtime.parent_pid()
        if parent:
            def _die() -> None:
                print("[runtime] 父进程已退出，helper 随之退出", flush=True)
                os._exit(0)
            threading.Thread(target=runtime.watch_parent, args=(parent, _die),
                             daemon=True).start()
    except Exception as e:
        print(f"[runtime] helper 看门狗未启动：{e}", flush=True)
    runpy.run_path(str(path), run_name="__main__")
    return 0


if "--feishu-bot" in sys.argv[1:]:
    sys.exit(_run_tool("feishu_bot"))
if "--capture-agent" in sys.argv[1:]:
    sys.exit(_run_tool("capture_agent"))

# 必须在 import app.config 之前生效：HOST 是模块级常量，导入后就改不动了
if "--lan" in sys.argv[1:]:
    os.environ["LIVENOTES_HOST"] = "0.0.0.0"


def _web_overlay_arg(argv: list[str]) -> str | None:
    """``--web-overlay DIR`` / ``--web-overlay=DIR``：桌面皮肤目录。

    网页版不传，行为不变；桌面壳开发态传 ``desktop/web``，打包态由引擎
    在 ``app/web-overlay`` 自动发现（见 app/webroot.py）。
    """
    for i, arg in enumerate(argv):
        if arg == "--web-overlay":
            if i + 1 >= len(argv):
                raise SystemExit("--web-overlay 需要一个目录参数")
            return argv[i + 1]
        if arg.startswith("--web-overlay="):
            value = arg.split("=", 1)[1].strip()
            if not value:
                raise SystemExit("--web-overlay 需要一个目录参数")
            return value
    return None


_overlay = _web_overlay_arg(sys.argv[1:])
if _overlay is not None:
    _overlay_path = Path(_overlay).expanduser().resolve()
    if not _overlay_path.is_dir():
        raise SystemExit("--web-overlay 目录不存在：%s" % _overlay_path)
    os.environ["LIVENOTES_WEB_OVERLAY"] = str(_overlay_path)

from app import live, logs, metrics, scheduler, seed_demo, store, runtime  # noqa: E402
from app import audio  # noqa: E402
from app.config import (BASE_URL, HOST, METRICS_SAMPLE_S, PORT, RESUME_ROOMS,  # noqa: E402
                        ensure_dirs)
from app.server import make_server, reconcile_rooms  # noqa: E402

BANNER = """
==========================================================================
  LiveNotes / 直播语记  ·  多人语音直播结构化记录原型
--------------------------------------------------------------------------
  本机打开 : http://127.0.0.1:{port}/      ← 点「实时监听」必须用这个地址
  手机访问 : {base}/    ← 只能看回放（手机浏览器做不了标签页采集）
  数据文件 : {db}
  实时监听 : 页面右上角「● 实时监听」→ 选择正在播放的直播标签页（勾选分享音频）

  【先看清这一条，再决定要不要投入算法预算】
  观众端只能拿到平台下混的一路音频。8~10 人「同时」发言时，
  从单路混音里稳定分离出每个人并逐一归属身份，目前没有可工程化的解。
  本原型对重叠段采取「标记 + 不转写 + 不进总结」，宁可留白不编造。
  真正打通的办法是拿主播端 / RTC 的 per-uid 分轨（详见 docs/01）。

  若使用公司自有直播间的服务端分轨，可直接走 per_uid 模式。
==========================================================================
"""


_HELPERS_STOP = threading.Event()
_HELPERS_LOCK = threading.Lock()


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _helpers_enabled() -> bool:
    """桌面壳用 LIVENOTES_HELPERS=1 让引擎顺带拉起飞书机器人和采集代理。

    网页版（tools/start.bat）自己起这两个进程，不设这个变量，行为不变。
    """
    return _env_on("LIVENOTES_HELPERS") and not _env_on("LIVENOTES_NO_HELPERS")


def _seed_feishu_config(data_dir: Path) -> None:
    """桌面数据目录没有 feishu.json 时，从仓库 data/ 复制一份。

    桌面版的数据目录是 %LOCALAPPDATA%\\LiveNotes\\data，和仓库 data/ 是两个地方；
    不复制的话，第一次用桌面版飞书机器人会因为没凭证直接退出。打包态没有仓库
    data/，这一步自然跳过，用户把 feishu.json 放进数据目录即可。
    """
    dst = data_dir / "feishu.json"
    src = ROOT / "data" / "feishu.json"
    if dst.is_file() or not src.is_file():
        return
    try:
        if src.resolve() == dst.resolve():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"[helpers] 已复制飞书凭证 {src} -> {dst}", flush=True)
    except OSError as e:
        print(f"[helpers] 复制飞书凭证失败：{e}", flush=True)


def _helper_cmd(name: str, extra: list[str] | None = None) -> list[str] | None:
    extra = extra or []
    if getattr(sys, "frozen", False):
        return [sys.executable, "--" + name.replace("_", "-")] + extra
    path = TOOLS / f"{name}.py"
    if not path.is_file():
        print(f"[helpers] 找不到 {path}，跳过 {name}", flush=True)
        return None
    # 开发态也经 run.py 的 _run_tool 分支：统一挂父进程看门狗，引擎死了
    # helper 不会变成孤儿。
    return [sys.executable, str(ROOT / "run.py"),
            "--" + name.replace("_", "-")] + extra


def _spawn_helper(cmd: list[str], env: dict[str, str], flags: int):
    env = dict(env)
    env["LIVENOTES_PARENT_PID"] = str(os.getpid())
    try:
        return subprocess.Popen(
            cmd, cwd=str(ROOT), env=env, creationflags=flags,
            stdin=subprocess.DEVNULL)
    except OSError as e:
        print(f"[helpers] 启动失败 {' '.join(cmd[:2])}: {e}", flush=True)
        return None


def _stop_proc(p) -> None:
    if p is None or p.poll() is not None:
        return
    # Windows 上 terminate() 只杀代理本身，它开的 Chrome/ffmpeg 会留在后台；
    # 用 taskkill /T 连子进程一起收掉。
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            p.terminate()
        except OSError:
            pass


def _capture_mode() -> str:
    """桌面采集方式：browser（应用内浏览器）/ direct（抖音直取）/ external（Chrome）。

    空值按 browser 处理；网页版不读这个偏好，仍由 tools/start.bat 的环境变量决定。
    """
    try:
        from app import settings as app_settings
        mode = str(app_settings.prefs().get("capture_mode") or "").strip().lower()
    except Exception:
        mode = ""
    return mode if mode in ("browser", "direct", "external") else "browser"


def _capture_agent_cmd(port: int, mode: str) -> list[str] | None:
    if mode == "browser":
        return None
    return _helper_cmd("capture_agent", ["--base", f"http://127.0.0.1:{port}"])


def _watch_capture_mode(port: int, env: dict[str, str], flags: int,
                        specs: dict[str, list[str]], procs: dict,
                        initial: str) -> None:
    """设置页改采集方式后，重启采集代理（或停掉，交给应用内浏览器）。

    有房间正在听时不立刻切：先等它们结束，避免切换把正在录的会话打断。
    """
    from app import rooms as app_rooms

    current = initial
    while not _HELPERS_STOP.wait(2):
        mode = _capture_mode()
        if mode == current:
            continue
        active = [r for r in app_rooms.list_rooms()
                  if r.get("state") in ("pending", "starting", "live")]
        if active:
            continue
        with _HELPERS_LOCK:
            old = procs.pop("capture", None)
            specs.pop("capture", None)
        if old is not None:
            _stop_proc(old)
        env["LIVENOTES_CAPTURE_MODE"] = (
            "direct" if mode == "direct" else "per_room")
        cmd = _capture_agent_cmd(port, mode)
        if cmd:
            fresh = _spawn_helper(cmd, env, flags)
            if fresh is not None:
                with _HELPERS_LOCK:
                    specs["capture"] = cmd
                    procs["capture"] = fresh
                print(f"[helpers] 采集方式 -> {mode}（pid={fresh.pid}）", flush=True)
            else:
                print(f"[helpers] 采集方式 -> {mode} 启动失败", flush=True)
        else:
            print("[helpers] 采集方式 -> browser（应用内浏览器）", flush=True)
        current = mode


def _supervise_helpers(specs: dict[str, list[str]], env: dict[str, str],
                       flags: int, procs: dict) -> None:
    """子进程挂了就重开；连续失败 5 次就放弃，避免日志里无限刷屏。"""
    fails: dict[str, int] = {}
    while not _HELPERS_STOP.wait(5):
        with _HELPERS_LOCK:
            items = list(specs.items())
            running = dict(procs)
        for name, cmd in items:
            p = running.get(name)
            if p is None or p.poll() is None:
                continue
            with _HELPERS_LOCK:
                # 采集方式切换时会被主动摘掉，别再把它拉起来。
                if name not in specs:
                    continue
                rc = p.returncode
                fails[name] = fails.get(name, 0) + 1
                if fails[name] > 5:
                    print(f"[helpers] {name} 连续退出（rc={rc}），不再重启", flush=True)
                    specs.pop(name, None)
                    procs.pop(name, None)
                    continue
            wait_s = min(5 * fails[name], 30)
            print(f"[helpers] {name} 退出（rc={rc}），{wait_s}s 后重启", flush=True)
            if _HELPERS_STOP.wait(wait_s):
                return
            fresh = _spawn_helper(cmd, env, flags)
            if fresh is not None:
                with _HELPERS_LOCK:
                    if name in specs:
                        procs[name] = fresh
                    else:
                        _stop_proc(fresh)


def _start_helpers(port: int) -> dict:
    if not _helpers_enabled():
        return {}
    from app.config import DATA_DIR

    _seed_feishu_config(DATA_DIR)
    env = os.environ.copy()
    env.update({
        "LIVENOTES_PORT": str(port),
        "LIVENOTES_HOST": "127.0.0.1",
        # 机器人/采集代理都直连本机引擎，不靠 LAN 地址猜端口。
        "LIVENOTES_API": f"http://127.0.0.1:{port}",
        # 飞书消息里的回听/分享链接也指向本机；桌面版本来就只监听 127.0.0.1。
        "LIVENOTES_BASE_URL": f"http://127.0.0.1:{port}",
        "LIVENOTES_DATA_DIR": str(DATA_DIR),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    specs: dict[str, list[str]] = {}

    has_cfg = (DATA_DIR / "feishu.json").is_file() or (
        os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"))
    if _env_on("LIVENOTES_NO_FEISHU"):
        if has_cfg:
            print("[helpers] LIVENOTES_NO_FEISHU=1：跳过飞书机器人", flush=True)
        has_cfg = False
    if has_cfg:
        cmd = _helper_cmd("feishu_bot")
        if cmd:
            specs["feishu"] = cmd
    elif not _env_on("LIVENOTES_NO_FEISHU"):
        print("[helpers] 没有 feishu.json，跳过飞书机器人；把凭证放到 "
              f"{DATA_DIR / 'feishu.json'} 后重启桌面端即可", flush=True)

    # 采集方式跟设置页走：browser = 桌面壳用应用内浏览器接房间（不拉代理）；
    # direct = 抖音直取（ffmpeg）；external = 网页版那套每房间一个 Chrome。
    mode = _capture_mode()
    env["LIVENOTES_CAPTURE_MODE"] = "direct" if mode == "direct" else "per_room"
    cmd = _capture_agent_cmd(port, mode)
    if cmd:
        specs["capture"] = cmd
        print(f"[helpers] 采集方式：{mode}", flush=True)
    else:
        print("[helpers] 采集方式：browser（应用内浏览器，不拉外部 Chrome）", flush=True)

    procs = {}
    for name, cmd in specs.items():
        p = _spawn_helper(cmd, env, flags)
        if p is not None:
            procs[name] = p
            print(f"[helpers] 已启动 {name}（pid={p.pid}）", flush=True)
    if procs:
        threading.Thread(target=_supervise_helpers,
                         args=(specs, env, flags, procs), daemon=True).start()
    threading.Thread(target=_watch_capture_mode,
                     args=(port, env, flags, specs, procs, mode),
                     daemon=True).start()
    return procs


def _stop_helpers(procs: dict) -> None:
    _HELPERS_STOP.set()
    for p in procs.values():
        _stop_proc(p)
    for p in procs.values():
        if p is None:
            continue
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except OSError:
                pass


def main() -> int:
    if "--reload" in sys.argv[1:]:
        # 交给守护进程去起真正的服务；它负责在"没有采集任务"时换代码
        return subprocess.call([sys.executable, str(ROOT / "tools" / "dev.py")])
    ensure_dirs()
    # 单实例：同一数据目录只允许一个引擎。锁被占但对方不健康时，runtime
    # 会确认是本应用进程后回收再重试；仍然失败就退出，让壳去复用已有引擎。
    ok, existing = runtime.acquire(PORT, HOST)
    if not ok:
        info = existing or {}
        print(f"[runtime] 已有引擎在跑：pid={info.get('pid')} "
              f"port={info.get('port')}，本进程退出", flush=True)
        return 3
    conn = store.connect()
    store.init_db(conn)
    n = store.list_sessions(conn)
    seeded = False
    if not n and os.environ.get("LIVENOTES_SEED_DEMO") == "1":
        seed_demo.seed(conn)
        seeded = True
    print(f"[init] 已有 {len(n) + (1 if seeded else 0)} 个场次；"
          f"FTS5 trigram = {store.FTS_TRIGRAM}", flush=True)
    conn.close()
    # 启动全局 LLM 调度器：把上次进程退出时没跑完的任务标成 failed（可重试），
    # 同时起固定数量的 worker。之后所有总结/润色/分类/收尾都排队走它。
    scheduler.SCHEDULER.start()
    # 指标采样与停滞巡检：必须在 init_db 之后启动，否则第一批快照写不进表。
    metrics.METRICS.start_sampler(METRICS_SAMPLE_S)
    live.start_watchdog()
    logs.log_event("app.start", db=str(store.DB_PATH), host=HOST, port=PORT,
                   sample_s=METRICS_SAMPLE_S)
    repaired = live.recover_spools()
    if repaired:
        logs.log_event("app.recovered_spools", count=repaired)
    # 房间状态收口：必须赶在 make_server 之前。采集代理只认 /api/rooms 返回的
    # 状态，第一拍看到 live/starting 就会重新拉起浏览器——服务重启不该等于
    # 「把上次的直播间再打开一遍」。默认落成「已中断」，要人点「重试」。
    if RESUME_ROOMS:
        print("[rooms] LIVENOTES_RESUME_ROOMS=1：自动恢复上次未结束的采集", flush=True)
    else:
        conn = store.connect()
        try:
            reconcile_rooms(conn)
        finally:
            conn.close()
    # 录音归档清理：原 WAV 按保留期删除、过期的归档/片段缓存清掉。
    # 启动时跑一次，之后每 6 小时一次；没有 ffmpeg 也不影响，只是不转码。
    audio.start_sweeper()
    audio.start_backfill()

    srv = make_server(HOST, PORT)
    print(BANNER.format(host=HOST, port=PORT, db=store.DB_PATH, base=BASE_URL), flush=True)
    if HOST in ("127.0.0.1", "localhost"):
        print("  [提示] 当前只监听本机，手机打不开推送里的链接。要手机能访问："
              "python run.py --lan", flush=True)
    if os.environ.get("LIVENOTES_NO_BROWSER", "").lower() not in ("1", "true", "yes"):
        try:
            # 必须用 127.0.0.1：浏览器只把 localhost/127.0.0.1 当安全上下文，
            # 0.0.0.0 和局域网 IP 下 getDisplayMedia 直接不存在，点监听会失败。
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        except Exception:
            pass
    # 桌面壳专用：飞书机器人 + 采集代理跟引擎一起起停。网页版不设这个变量，
    # 仍由 tools/start.bat 分别拉起，互不影响。
    helpers = _start_helpers(PORT)
    # 父进程看门狗：壳被硬终止（关终端、任务管理器、崩溃）时，正常退出路径
    # 不会执行；这里每 2 秒看一眼父进程，没了就关服务，走 finally 收尾。
    parent_watch_stop = threading.Event()
    parent = runtime.parent_pid()
    if parent:
        def _parent_gone() -> None:
            print("[runtime] 父进程已退出，引擎停止服务", flush=True)
            srv.shutdown()
        threading.Thread(target=runtime.watch_parent,
                         args=(parent, _parent_gone, 2.0, parent_watch_stop),
                         daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[exit] 已停止。数据保留在 data/livenotes.db", flush=True)
    finally:
        parent_watch_stop.set()
        _stop_helpers(helpers)
        live.shutdown()                 # 停止采集，并把各场的收尾任务排进调度器
        scheduler.SCHEDULER.shutdown(grace=5)   # 给正在跑的模型请求 5 秒收尾
        metrics.METRICS.stop_sampler()
        logs.log_event("app.stop")
        srv.server_close()
        runtime.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
