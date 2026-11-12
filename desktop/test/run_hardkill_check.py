"""硬杀验收：模拟壳崩溃（TerminateProcess，不杀子进程树），确认引擎/helper 不残留。

壳把引擎挂进 Job Object（KILL_ON_JOB_CLOSE），壳进程消失时内核负责收掉整棵
进程树；引擎自己还有父进程看门狗兜底。两者任一生效都应零残留。

用法（仓库根目录）：
    python desktop/test/run_hardkill_check.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop" / "test"))

import psutil  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ui = load("run_ui_shots", ROOT / "desktop" / "test" / "run_ui_shots.py")


def tree(root_pid: int) -> list[dict]:
    try:
        root = psutil.Process(root_pid)
    except psutil.NoSuchProcess:
        return []
    out = []
    for p in [root] + root.children(recursive=True):
        try:
            out.append({"pid": p.pid, "ppid": p.ppid(), "name": p.name(),
                        "ctime": p.create_time(),
                        "cmd": " ".join(p.cmdline())[:160]})
        except psutil.Error:
            pass
    return out


def alive(item: dict) -> bool:
    try:
        p = psutil.Process(item["pid"])
        return abs(p.create_time() - item["ctime"]) < 0.01
    except psutil.Error:
        return False


def wait_engine(home: Path, proc: subprocess.Popen, timeout: float) -> dict | None:
    """等壳把引擎拉起来：engine.json 出现且 /api/health 能应答。"""
    deadline = time.time() + timeout
    info_path = home / "data" / "runtime" / "engine.json"
    while time.time() < deadline and proc.poll() is None:
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("pid") and info.get("port"):
                return info
        except (OSError, ValueError):
            pass
        time.sleep(0.4)
    return None


def main() -> int:
    if not ui.EXE.exists():
        ui.log(f"missing exe: {ui.EXE}")
        return 2
    ui.RUN_DIR.mkdir(parents=True, exist_ok=True)
    spike = ui.load_spike()
    spike.HOME = ui.HOME
    spike.prepare_home()
    # 采集方式 direct 才会拉起 capture_agent helper；browser 模式下没有 helper
    # 可验，硬杀测试就只剩引擎一条进程。
    (ui.HOME / "data" / "settings.json").write_text(
        json.dumps({"version": 2, "profiles": {},
                    "prefs": {"capture_mode": "direct"}}), encoding="utf-8")
    try:
        (ui.HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass
    run_exe = ui.RUN_DIR / ui.EXE.name
    shutil.copy2(ui.EXE, run_exe)

    env = os.environ.copy()
    env.update({
        "LIVENOTES_HOME": str(ui.HOME),
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_ALLOW_MULTI": "1",
        "LIVENOTES_CLOSE_TO_TRAY": "0",
        "LIVENOTES_NO_FEISHU": "1",
        "LIVENOTES_HELPERS": "1",
        "LIVENOTES_AUTO_EXIT_MS": "120000",   # 只要别在测试中途正常退出
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (ui.RUN_DIR / "hardkill.out").open("wb")
    proc = subprocess.Popen([str(run_exe)], cwd=str(ui.RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        info = wait_engine(ui.HOME, proc, 40)
        if not info:
            ui.log("engine did not come up")
            return 1
        time.sleep(2.0)                      # 等 helper 也起来
        seen = {p["pid"]: p for p in tree(proc.pid)}
        engine_pid = int(info["pid"])
        ui.log(f"shell={proc.pid} engine={engine_pid} "
               f"helpers={[p['pid'] for p in seen.values() if p['name'] == 'python.exe' and p['pid'] != engine_pid]}")

        ui.log("hard kill shell (TerminateProcess, no /T)")
        proc.kill()                          # 只杀壳，不碰子进程
        proc.wait(timeout=10)

        deadline = time.time() + 15
        while time.time() < deadline:
            if not any(alive(p) for p in seen.values() if p["pid"] != proc.pid):
                break
            time.sleep(0.5)

        survivors = [p for p in seen.values()
                     if p["pid"] != proc.pid and alive(p)]
        py = [p for p in survivors if p["name"] in ("python.exe", "pythonw.exe")]
        ui.log("survivors: " + (", ".join(
            f'{p["name"]}:{p["pid"]}' for p in survivors) or "none"))
        ui.log("python survivors: " + (", ".join(
            f'{p["name"]}:{p["pid"]}' for p in py) or "none"))
        for item in survivors:
            try:
                psutil.Process(item["pid"]).kill()
            except psutil.Error:
                pass
        return 0 if not py else 1
    finally:
        if proc.poll() is None:
            proc.kill()
        out.close()


if __name__ == "__main__":
    sys.exit(main())
