# -*- coding: utf-8 -*-
"""结构化日志：一行一个 JSON，终端同时保留人读版本。

为什么不用 logging 模块再包一层：这个项目没有日志配置中心，stdout 就是
运维窗口。这里做三件小事就够：
  - 每条日志带 ts / level / event / session_id 与任意字段，能直接 grep；
  - 按天落 ``data/logs/app-YYYYMMDD.jsonl``，单文件超过 20MB 轮转一份；
  - warn / error 同时写进 SQLite 的 ``metric_events``，看板能列出最近事件，
    进程重启后也还在。

写日志永远不能影响业务：所有文件 / 数据库异常都在这里吞掉。
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
MAX_BYTES = 20 * 1024 * 1024
KEEP_DAYS = 7
# 测试可以关掉；正常运行时 warn/error 必须进事件表，否则"事后查不到线索"。
PERSIST_EVENTS = True
LEVELS = ("debug", "info", "warn", "error")

_lock = threading.Lock()
_last_cleanup = 0.0


def _human(rec: dict) -> str:
    """终端里的一行：时间 + 级别 + 事件 + 少量关键字段。"""
    ts = rec["ts"][5:19].replace("T", " ")
    parts = ["[%s] %-5s %s" % (ts, rec["level"].upper(), rec["event"])]
    for k, v in rec.items():
        if k in ("ts", "level", "event"):
            continue
        s = str(v).replace("\n", " ")
        if len(s) > 160:
            s = s[:157] + "..."
        parts.append("%s=%s" % (k, s))
    return " ".join(parts)


def _path(day: datetime) -> Path:
    return LOG_DIR / ("app-%s.jsonl" % day.strftime("%Y%m%d"))


def _write(day: datetime, line: str) -> None:
    global _last_cleanup
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _path(day)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            try:
                os.replace(path, rotated)
            except OSError:
                pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if time.time() - _last_cleanup > 3600:
            _last_cleanup = time.time()
            cutoff = time.time() - KEEP_DAYS * 86400
            for p in LOG_DIR.glob("app-*.jsonl*"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def _persist(rec: dict) -> None:
    if not PERSIST_EVENTS:
        return
    try:
        from . import store
        fields = {k: v for k, v in rec.items()
                  if k not in ("ts", "level", "event", "session_id")}
        store.record_metric_event(rec["level"], rec["event"],
                                  session_id=str(rec.get("session_id") or ""),
                                  fields=fields, ts=rec["ts"])
    except Exception:
        pass


def log_event(event: str, level: str = "info", msg: str = "",
              session_id: str = "", **fields: Any) -> None:
    """记一条结构化日志。字段值会原样进 JSON；不可序列化的转字符串。"""
    level = str(level or "info").lower()
    if level not in LEVELS:
        level = "info"
    now = datetime.now().astimezone()
    rec: dict[str, Any] = {
        "ts": now.isoformat(timespec="milliseconds"),
        "level": level,
        "event": str(event),
    }
    if session_id:
        rec["session_id"] = str(session_id)
    if msg:
        rec["msg"] = str(msg)
    for k, v in fields.items():
        if v is not None:
            rec[k] = v
    line = json.dumps(rec, ensure_ascii=False, default=str, separators=(",", ":"))
    print(_human(rec), flush=True)
    _write(now, line)
    if level in ("warn", "error"):
        _persist(rec)


def read_recent(limit: int = 50, day: datetime | None = None) -> list[dict]:
    """读当天 JSONL 的末尾若干条，给本地排障用；文件不存在返回空。"""
    path = _path(day or datetime.now())
    if not path.is_file():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            raw = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw[-max(1, int(limit)):]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def cleanup(days: int = KEEP_DAYS) -> int:
    """手动清理过期日志（测试和运维脚本用）。返回删除的文件数。"""
    if not LOG_DIR.is_dir():
        return 0
    cutoff = time.time() - max(1, int(days)) * 86400
    n = 0
    for p in LOG_DIR.glob("app-*.jsonl*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n
