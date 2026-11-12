# -*- coding: utf-8 -*-
"""桌面诊断验收（D6）：断流、房间视图崩溃、引擎崩溃与诊断包。

不依赖壳自报成功：从壳日志、``logs/diag.jsonl``、引擎 HTTP、导出的 zip
四个外部侧取证。流程：

1. 起本地测试站，再起壳（``LIVENOTES_SPIKE=d3crash``）；
2. 壳自动开房间 A，脚本确认 /api/diagnostics 有在跑的场次；
3. 壳用 CDP ``Page.crash`` 真崩一次渲染进程 → 等 ``room_process_failed``；
4. 壳关掉房间 A（引擎会话还在，只是没有音频块）→ 等 ``stream_stalled``；
5. 杀掉引擎进程 → 等 ``engine_exited``，并检查自动导出的诊断包；
6. 收尾，打印 PASS / FAIL。

    python desktop/test/verify_diag.py
    python desktop/test/verify_diag.py --shell desktop/src-tauri/target/debug/livenotes-desktop.exe
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SITE_PORT = 8899


def find_shell(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise SystemExit("找不到指定的壳 exe：%s" % p)
    env = os.environ.get("LIVENOTES_SHELL_EXE")
    if env and Path(env).is_file():
        return Path(env)
    candidates = [
        ROOT / "desktop/src-tauri/target/debug/livenotes-desktop.exe",
        ROOT / "desktop/src-tauri/target/release/livenotes-desktop.exe",
    ]
    for p in candidates:
        if p.is_file():
            return p
    raise SystemExit("找不到壳 exe；用 --shell 指定，或先 cargo build")


def log_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def read_diag(path: Path) -> list[dict]:
    out = []
    text = log_text(path)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def wait_until(fn, timeout: float, desc: str, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = fn()
        except Exception as e:  # noqa: BLE001 - 轮询期间任何异常都当作“还没好”
            last = None
            _ = e
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError("等待超时：%s（最后结果 %r）" % (desc, last))


def api(port: int, path: str, timeout: float = 6.0):
    with urllib.request.urlopen(
            "http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
        return json.load(r)


def port_open(host: str, port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def listening_pid(port: int) -> int | None:
    ps = ("(Get-NetTCPConnection -LocalPort %d -State Listen "
          "| Select-Object -First 1 -ExpandProperty OwningProcess)" % port)
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=25,
        ).stdout.strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass
    # PowerShell 不可用/没权限时退回 netstat
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True,
                             text=True, timeout=20).stdout
        for line in out.splitlines():
            if (":%d " % port) in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    return int(parts[-1])
    except Exception:
        pass
    return None


def check_zip(path: Path) -> dict:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        required = {
            "README.txt", "summary.json", "diag/events.json",
            "snapshots/health.json", "snapshots/diagnostics.json",
            "logs/shell.log", "logs/engine.log",
        }
        missing = required - names
        if missing:
            raise AssertionError("诊断包缺少 %s：%s" % (sorted(missing), path))
        # 这两个原始接口分别带 last_line（字幕片段）和未脱敏的直播间 URL。
        forbidden = {"snapshots/live-list.json", "snapshots/rooms.json"} & names
        if forbidden:
            raise AssertionError("诊断包含敏感快照 %s：%s" % (sorted(forbidden), path))
        summary = json.loads(zf.read("summary.json").decode("utf-8"))
        events = json.loads(zf.read("diag/events.json").decode("utf-8"))
        health = json.loads(zf.read("snapshots/health.json").decode("utf-8"))
        snap_blob = b"".join(
            zf.read(n) for n in sorted(names)
            if n.startswith("snapshots/") or n == "summary.json")
    return {"path": path, "summary": summary, "events": events,
            "health": health, "names": names,
            "privacy_ok": b"last_line" not in snap_blob}


def main() -> int:
    # Windows 管道下默认是 GBK，中文和 ✓/✗ 会乱码或抛 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", help="壳 exe 路径")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    shell = find_shell(args.shell)
    # 必须放在工作目录里：桌面壳在本机沙箱下对系统 %TEMP% 没有写权限
    # （create ...\webview2\splash: 拒绝访问），放在仓库内才跑得起来。
    base = Path(tempfile.mkdtemp(prefix=".diag-verify-", dir=str(ROOT)))
    home = base / "home"
    exports = base / "exports"
    home.mkdir()
    exports.mkdir()
    site_proc = None
    shell_proc = None
    ok = True
    results: list[tuple[str, bool, str]] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and bool(cond)
        results.append((name, bool(cond), detail))
        print(("PASS  " if cond else "FAIL  ") + name + (("  · " + detail) if detail else ""))

    print("shell :", shell)
    print("home  :", home)
    print("export:", exports)
    if not (HERE / "speech.mp4").is_file():
        print("缺少 desktop/test/speech.mp4（先跑 make_media.ps1）", file=sys.stderr)
        return 2

    if not port_open("127.0.0.1", SITE_PORT):
        site_proc = subprocess.Popen(
            [sys.executable, str(HERE / "site_server.py")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT))
        wait_until(lambda: port_open("127.0.0.1", SITE_PORT), 15,
                   "测试站 8899 启动")
    print("site  : 127.0.0.1:%d" % SITE_PORT)

    env = os.environ.copy()
    # 不把用户真实的 LLM Key / 飞书配置带进测试引擎
    for k in list(env):
        if k.startswith("LIVENOTES_LLM") or k.startswith("LIVENOTES_FEISHU"):
            env.pop(k, None)
    env.update({
        "LIVENOTES_HOME": str(home),
        "LIVENOTES_DIAG_DIR": str(exports),
        "LIVENOTES_DIAG_AUTO_EXPORT": "1",
        # 测试站是循环播放的短片，语句之间有空隙；阈值太小会把正常停顿误判成断流。
        # 取 8 秒，真正关掉采集页后 8 秒内必然触发。
        "LIVENOTES_SHELL_STALL_SECONDS": "8",
        "LIVENOTES_SHELL_POLL_MS": "1000",
        "LIVENOTES_SPIKE": "d3crash",
        "LIVENOTES_PUSH": "0",
        # 测试只关心壳的托盘告警，不让引擎自己的看门狗提前收尾/发飞书
        "LIVENOTES_STALL_SECONDS": "0",
        "LIVENOTES_STALL_WARN_SECONDS": "0",
        "LIVENOTES_ASR_WORKERS": "1",
        "LIVENOTES_ASR_THREADS": "1",
    })
    shell_log = home / "logs" / "shell.log"
    diag_log = home / "logs" / "diag.jsonl"
    engine_log = home / "logs" / "engine.log"

    try:
        shell_proc = subprocess.Popen(
            [str(shell)], env=env, cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("壳 pid:", shell_proc.pid)

        m = wait_until(
            lambda: re.findall(r"engine ready on 127\.0\.0\.1:(\d+)",
                               log_text(shell_log)),
            args.timeout, "engine ready")
        port = int(m[-1])
        print("engine:", port)
        check("引擎就绪", True, "127.0.0.1:%d" % port)

        diag = wait_until(lambda: api(port, "/api/diagnostics"),
                          args.timeout, "/api/diagnostics")
        check("/api/diagnostics 可用",
              diag.get("ok") is True and "live" in diag and "rooms" in diag,
              "engine pid=%s" % (diag.get("engine") or {}).get("pid"))

        wait_until(lambda: "room_open ok label=room-a" in log_text(shell_log),
                   args.timeout, "房间 A 打开")
        check("房间 A 打开", True)

        # 等采集中真的有音频块（idle_s 很小），证明确实是“断流”而不是没开起来
        def flowing():
            rows = api(port, "/api/diagnostics").get("live") or []
            for r in rows:
                if r.get("idle_s") is not None and float(r["idle_s"]) < 3.0:
                    return r
            return None
        row = wait_until(flowing, 60, "房间 A 开始出音频块")
        sid = row.get("id")
        check("采集链路出块", bool(sid), "session=%s" % sid)

        # 3) 房间视图崩溃
        ev = wait_until(
            lambda: next((e for e in read_diag(diag_log)
                          if e.get("kind") == "room_process_failed"), None),
            90, "room_process_failed")
        crash_epoch = int(ev.get("epoch") or 0)
        check("房间视图崩溃被捕获", True,
              "%s auto_reload=%s" % ((ev.get("detail") or {}).get("kind"),
                                     (ev.get("detail") or {}).get("auto_reload")))
        wait_until(lambda: "reloaded after process failure" in log_text(shell_log),
                   30, "房间视图自动重载")
        check("房间视图自动重载", True)

        # 4) 关掉房间视图，引擎会话还在 → 断流。
        # 崩溃本身也会让块停一会儿，所以只认「关闭房间之后」记下的断流事件。
        def close_epoch():
            for line in log_text(shell_log).splitlines():
                if "d3crash: closing room-a to stop chunks" in line:
                    m = re.match(r"\[(\d+)\]", line)
                    if m:
                        return int(m.group(1))
            return None
        closed_at = wait_until(close_epoch, 90, "房间 A 关闭")
        ev = wait_until(
            lambda: next((e for e in read_diag(diag_log)
                          if e.get("kind") == "stream_stalled"
                          and int(e.get("epoch") or 0) >= closed_at), None),
            90, "关闭后的 stream_stalled")
        check("断流被捕获", True,
              "session=%s idle=%ss" % ((ev.get("detail") or {}).get("session_id"),
                                       (ev.get("detail") or {}).get("idle_s")))

        def bundle_with(kinds: set[str]):
            for p in sorted(exports.glob("LiveNotes-diagnostics-*.zip"),
                            key=lambda p: p.stat().st_mtime):
                try:
                    z = check_zip(p)
                except Exception:
                    continue
                have = {e.get("kind") for e in z["events"]}
                if kinds <= have:
                    return z
            return None
        z0 = wait_until(
            lambda: bundle_with({"room_process_failed", "stream_stalled"}),
            60, "包含崩溃与断流的诊断包")
        check("诊断包结构完整", True,
              "%s（%d 个文件）" % (z0["path"].name, len(z0["names"])))
        check("诊断包 summary 可解析",
              (z0["summary"].get("shell") or {}).get("version") is not None, "")
        check("诊断包不含字幕片段", z0["privacy_ok"], "")

        # 5) 杀引擎，验证托盘/诊断包的“引擎已退出”路径。
        # 自动导出有 5 秒节流：上一个诊断包刚写完后立刻杀引擎，会被节流掉。
        # 等过节流窗口，保证 engine_exited 这一次能落一个包（总共第 3 个）。
        time.sleep(6)
        pid = wait_until(lambda: listening_pid(port), 20, "引擎监听 PID")
        print("engine pid:", pid)
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=20)
        ev = wait_until(
            lambda: next((e for e in read_diag(diag_log)
                          if e.get("kind") == "engine_exited"), None),
            45, "engine_exited")
        check("引擎退出被捕获", True, str((ev.get("detail") or {}).get("exit_code")))

        def exported_after_exit():
            for p in sorted(exports.glob("LiveNotes-diagnostics-*.zip"),
                            key=lambda p: p.stat().st_mtime):
                try:
                    z = check_zip(p)
                except Exception:
                    continue
                state = (z["summary"].get("engine") or {}).get("state")
                kinds = {e.get("kind") for e in z["events"]}
                if state == "exited" and "engine_exited" in kinds:
                    return z
            return None
        z1 = wait_until(exported_after_exit, 45, "引擎退出后的诊断包")
        health_err = bool(z1["health"].get("error") or z1["health"].get("http"))
        check("引擎退出后仍能导出诊断包", True, z1["path"].name)
        check("退出后快照记录引擎不可达", health_err, str(z1["health"])[:120])
        check("诊断包不含 LLM Key",
              "LIVENOTES_LLM_KEY" not in json.dumps(z1["summary"], ensure_ascii=False),
              "")
        check("退出后的诊断包同样不含字幕片段", z1["privacy_ok"], "")
    except Exception as e:
        check("验收流程", False, "%s: %s" % (type(e).__name__, e))
        print("\n--- shell.log tail ---")
        print("\n".join(log_text(shell_log).splitlines()[-30:]))
        print("\n--- engine.log tail ---")
        print("\n".join(log_text(engine_log).splitlines()[-30:]))
        print("\n--- diag.jsonl tail ---")
        print("\n".join(log_text(diag_log).splitlines()[-15:]))
    finally:
        if shell_proc and shell_proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(shell_proc.pid), "/T", "/F"],
                           capture_output=True, text=True, timeout=30)
        if site_proc and site_proc.poll() is None:
            site_proc.terminate()
            try:
                site_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                site_proc.kill()
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)
        else:
            print("保留现场：", base)

    print("\n结果：", "PASS" if ok else "FAIL")
    for name, good, detail in results:
        print(("  ✓ " if good else "  ✗ ") + name +
              (("  " + detail) if detail else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
