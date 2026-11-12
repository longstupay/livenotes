# -*- coding: utf-8 -*-
"""引擎运行期状态：单实例锁 + engine.json + 父进程看门狗。

桌面壳每次启动都会挑一个临时端口拉起引擎。如果壳被硬终止（关终端、
任务管理器、崩溃），引擎和它拉起的飞书/采集 helper 不会自己退出，
下次启动就会出现两套引擎共用同一个库、飞书重复回复。

这里做三件事：
1. `engine.lock` 独占锁：同一数据目录只允许一个引擎；
2. `engine.json` 记录 pid / port / started_at，壳启动时先读它，健康就复用；
3. 锁被占但对方不健康时，确认是本应用的进程后回收，再重新抢锁。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from .config import DATA_DIR, PORT as CONFIG_PORT

RUNTIME_DIR = DATA_DIR / "runtime"
ENGINE_JSON = RUNTIME_DIR / "engine.json"
LOCK_PATH = RUNTIME_DIR / "engine.lock"

STARTED_AT = time.time()
_lock_file = None


def _lock(f) -> None:
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f) -> None:
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def read_info() -> dict[str, Any] | None:
    try:
        raw = json.loads(ENGINE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_info(info: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ENGINE_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ENGINE_JSON)


def _healthy(info: dict[str, Any]) -> bool:
    """engine.json 里的引擎还活着、而且确实是同一个进程。"""
    try:
        port = int(info.get("port") or 0)
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if not port or not pid:
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.2) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False
    # 旧版引擎没有 pid 字段；有就要求一致，避免端口被别的进程复用。
    got = body.get("pid")
    return got is None or int(got) == pid


def _process_name(pid: int) -> str:
    if os.name != "nt":
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                return f.read().strip().lower()
        except OSError:
            return ""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(h)


def _same_app(pid: int) -> bool:
    """只回收本应用的进程，避免 PID 复用时误杀别人的程序。"""
    return _process_name(pid) in ("python.exe", "pythonw.exe", "engine.exe")


def _terminate(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid() or not _same_app(pid):
        return False
    try:
        if os.name != "nt":
            import signal
            os.kill(pid, signal.SIGTERM)
            return True
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=8)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def acquire(port: int, host: str) -> tuple[bool, dict[str, Any] | None]:
    """抢单实例锁。成功返回 (True, info)，失败返回 (False, 占用者信息)。

    锁被占但占用者不健康时，最多回收重试一次；仍失败就交给壳去复用/报错。
    """
    global _lock_file
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for attempt in (0, 1):
        f = open(LOCK_PATH, "a+b")
        try:
            _lock(f)
        except OSError:
            f.close()
            info = read_info()
            if attempt == 0 and info and not _healthy(info):
                print(f"[runtime] 发现残留引擎 pid={info.get('pid')}，回收后重试",
                      flush=True)
                if _terminate(int(info.get("pid") or 0)):
                    time.sleep(1.0)
                    continue
            return False, info
        info = {
            "pid": os.getpid(),
            "port": port,
            "host": host,
            "started_at": STARTED_AT,
            "data_dir": str(DATA_DIR),
            "version": 1,
        }
        _write_info(info)
        _lock_file = f
        return True, info
    return False, read_info()


def release() -> None:
    global _lock_file
    try:
        ENGINE_JSON.unlink()
    except OSError:
        pass
    if _lock_file is not None:
        _unlock(_lock_file)
        try:
            _lock_file.close()
        except OSError:
            pass
        _lock_file = None


def health_fields() -> dict[str, Any]:
    return {"pid": os.getpid(), "port": CONFIG_PORT, "started_at": STARTED_AT}


# ---------------------------------------------------------------- 父进程看门狗

def parent_pid() -> int:
    raw = os.environ.get("LIVENOTES_PARENT_PID", "")
    if not raw:
        argv = sys.argv[1:]
        if "--parent-pid" in argv:
            i = argv.index("--parent-pid")
            raw = argv[i + 1] if i + 1 < len(argv) else ""
    try:
        return int(raw or 0)
    except ValueError:
        return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        err = ctypes.get_last_error()
        # 87 = ERROR_INVALID_PARAMETER：PID 已经不存在（进程退出、句柄全部关闭）。
        # 5 = ERROR_ACCESS_DENIED：进程还在但权限更高，保守认为还活着。
        return err != 87
    try:
        return kernel32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(h)


def watch_parent(pid: int, on_death, interval: float = 2.0,
                 stop_event: "threading.Event | None" = None) -> None:
    """父进程消失后执行 on_death；调用方负责起线程。"""
    if pid <= 0:
        return
    while True:
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)
        if not pid_alive(pid):
            on_death()
            return
