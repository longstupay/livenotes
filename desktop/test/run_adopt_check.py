"""接管验收：先手动起一个引擎，再启动壳，确认壳复用而不是再拉一个。

这是「上次壳崩了 / 网页版引擎还在跑 / 升级前残留」的恢复路径：壳读
data/runtime/engine.json -> /api/health 校验 pid -> 直接接管；退出时连
接管来的引擎一起收掉。

用法（仓库根目录）：
    python desktop/test/run_adopt_check.py
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


def engine_pids() -> set[int]:
    out = set()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
        except psutil.Error:
            continue
        if p.info["name"] in ("python.exe", "pythonw.exe") and "run.py" in cmd:
            out.add(p.info["pid"])
    return out


def wait_info(home: Path, timeout: float, pid: int | None = None) -> dict | None:
    path = home / "data" / "runtime" / "engine.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            if info.get("pid") and (pid is None or int(info["pid"]) == pid):
                return info
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    return None


def main() -> int:
    if not ui.EXE.exists():
        ui.log(f"missing exe: {ui.EXE}")
        return 2
    ui.RUN_DIR.mkdir(parents=True, exist_ok=True)
    spike = ui.load_spike()
    spike.HOME = ui.HOME
    spike.prepare_home()
    data = ui.HOME / "data"
    shutil.rmtree(data / "runtime", ignore_errors=True)   # 上次硬杀留下的 engine.json
    try:
        (ui.HOME / "webview2" / "main" / "EBWebView" / "DevToolsActivePort").unlink()
    except OSError:
        pass

    base_env = os.environ.copy()
    base_env.update({
        "LIVENOTES_HOME": str(ui.HOME),
        "LIVENOTES_DATA_DIR": str(data),
        "LIVENOTES_RESUME_ROOMS": "0",
        "LIVENOTES_NO_BROWSER": "1",
        "LIVENOTES_NO_HELPERS": "1",
        "LIVENOTES_NO_FEISHU": "1",
        "LIVENOTES_PORT": "61011",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    })

    engine_out = (ui.RUN_DIR / "adopt-engine.out").open("wb")
    shell_out = None
    engine = subprocess.Popen([sys.executable, str(ROOT / "run.py")],
                              cwd=str(ROOT), env=base_env,
                              stdout=engine_out, stderr=subprocess.STDOUT)
    shell = None
    try:
        info = wait_info(ui.HOME, 40, pid=engine.pid)
        if not info:
            ui.log(f"manual engine did not start (rc={engine.poll()})")
            return 1
        engine_pid = int(info["pid"])
        ui.log(f"manual engine pid={engine_pid} port={info.get('port')}")
        if engine_pid != engine.pid:
            ui.log(f"engine.json pid mismatch: {engine_pid} != {engine.pid}")
            return 1

        run_exe = ui.RUN_DIR / ui.EXE.name
        shutil.copy2(ui.EXE, run_exe)
        env = base_env.copy()
        env.pop("LIVENOTES_PORT", None)
        env.pop("LIVENOTES_DATA_DIR", None)
        env.update({
            "LIVENOTES_ALLOW_MULTI": "1",
            "LIVENOTES_CLOSE_TO_TRAY": "0",
            "LIVENOTES_AUTO_EXIT_MS": "12000",
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS":
                "--remote-debugging-port=0 --remote-allow-origins=*",
        })
        shell_out = (ui.RUN_DIR / "adopt-shell.out").open("wb")
        shell = subprocess.Popen([str(run_exe)], cwd=str(ui.RUN_DIR), env=env,
                                 stdout=shell_out, stderr=subprocess.STDOUT)

        time.sleep(7.0)
        after = wait_info(ui.HOME, 5, pid=engine_pid)
        pids = engine_pids()
        ui.log(f"after shell start: engine.json pid={after and after.get('pid')} "
               f"run.py pids={sorted(pids)}")
        adopted = bool(after and int(after["pid"]) == engine_pid)
        if not adopted:
            ui.log("FAIL: shell did not adopt the existing engine")
            return 1
        if pids != {engine_pid}:
            ui.log(f"FAIL: expected exactly one engine, got {sorted(pids)}")
            return 1

        code = shell.wait(timeout=40)
        ui.log(f"shell exited code={code}")
        deadline = time.time() + 15
        while time.time() < deadline and engine.pid in engine_pids():
            time.sleep(0.4)
        left = engine_pids()
        ui.log("engines after shell exit: " + (", ".join(map(str, sorted(left))) or "none"))
        if left:
            for pid in left:
                try:
                    psutil.Process(pid).kill()
                except psutil.Error:
                    pass
            return 1
        try:
            engine.wait(timeout=3)
        except subprocess.TimeoutExpired:
            engine.kill()
        return 0
    finally:
        for p in (shell, engine):
            if p is not None and p.poll() is None:
                try:
                    psutil.Process(p.pid).kill()
                except psutil.Error:
                    pass
        engine_out.close()
        if shell_out is not None:
            shell_out.close()


if __name__ == "__main__":
    sys.exit(main())
