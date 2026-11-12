"""退出回收验收：走正常 AppHandle::exit 路径，确认引擎/helper 不留孤儿。

隔离数据目录 + 独立实例，AUTO_EXIT_MS 触发和托盘「退出」相同的 Exit 流程。
用法（仓库根目录）：
    python desktop/test/run_exit_check.py
"""

from __future__ import annotations

import importlib.util
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


def main() -> int:
    if not ui.EXE.exists():
        ui.log(f"missing exe: {ui.EXE}")
        return 2
    ui.RUN_DIR.mkdir(parents=True, exist_ok=True)
    spike = ui.load_spike()
    spike.HOME = ui.HOME
    spike.prepare_home()
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
        "LIVENOTES_AUTO_EXIT_MS": "14000",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
            "--remote-debugging-port=0 --remote-allow-origins=*",
    })
    out = (ui.RUN_DIR / "exit.out").open("wb")
    proc = subprocess.Popen([str(run_exe)], cwd=str(ui.RUN_DIR), env=env,
                            stdout=out, stderr=subprocess.STDOUT)
    try:
        seen: dict[int, dict] = {}
        deadline = time.time() + 30
        while time.time() < deadline and proc.poll() is None:
            for item in tree(proc.pid):
                seen[item["pid"]] = item
            time.sleep(0.5)
        ui.log("before exit: " + ", ".join(
            f'{p["name"]}:{p["pid"]}<-{p["ppid"]}' for p in sorted(seen.values(), key=lambda x: x["pid"])))
        for p in sorted(seen.values(), key=lambda x: x["pid"]):
            if p["name"] == "python.exe":
                ui.log(f'python {p["pid"]} ppid={p["ppid"]} cmd={p["cmd"]}')
        try:
            code = proc.wait(timeout=45)
        except subprocess.TimeoutExpired:
            ui.log("app did not exit within 45s")
            return 1
        ui.log(f"app exited code={code}")
        time.sleep(1.5)

        survivors = []
        for item in seen.values():
            if item["pid"] == proc.pid:
                continue
            try:
                p = psutil.Process(item["pid"])
                if abs(p.create_time() - item["ctime"]) < 0.01:
                    survivors.append(item)
            except psutil.Error:
                pass
        ui.log("survivors: " + (", ".join(
            f'{p["name"]}:{p["pid"]}' for p in survivors) or "none"))
        for item in survivors:
            try:
                psutil.Process(item["pid"]).kill()
            except psutil.Error:
                pass
        return 0 if not survivors else 1
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, text=True)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
