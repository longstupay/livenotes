# -*- coding: utf-8 -*-
"""分享包：把一场的摘要、字幕、音频打成一个可上传妙记/外发的目录或 zip。

飞书妙记没有稳定开放的「上传即建妙记」接口，所以 P2 先把「人要去妙记点上传」
这一步做成一个包：summary.md + transcript.srt + audio.m4a + README。
上传完把妙记链接回填给机器人（/妙记），之后 /回听 优先给这个链接。
"""
from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path

from . import audio, export, store
from .config import SHARE_DIR


def _readme(s: dict, audio_name: str, minutes_url: str = "") -> str:
    lines = [
        "# 分享包 · %s" % (s.get("title") or s.get("id") or ""),
        "",
        "- 场次：`%s`" % s.get("id"),
        "- 开始：%s" % (s.get("started_at") or "-"),
        "- 时长：%.0f 秒" % float(s.get("duration_s") or 0),
        "- 音频：%s" % (audio_name or "（没有可用录音）"),
        "",
        "## 怎么用",
        "",
        "1. 打开飞书 → 妙记 → 上传，选择本目录里的音频文件。",
        "2. 转写完成后，把妙记分享链接发回机器人：`/妙记 CH01 <链接>`。",
        "3. 以后发 `/回听 CH01` 会优先返回妙记链接。",
        "",
        "## 注意",
        "",
        "- 妙记会重新转写一遍，术语和说话人可能与本包的 `transcript.srt` 不同；",
        "  需要精确文字时以 `transcript.srt` 为准。",
        "- 音频含第三方主播原声，分享范围和有效期请按实际情况设置。",
    ]
    if minutes_url:
        lines += ["", "已回填的妙记链接：%s" % minutes_url]
    return "\n".join(lines) + "\n"


def build_package(conn, sid: str, out_dir: Path | str | None = None,
                  include_audio: bool = True) -> dict:
    """生成分享包目录，返回 {dir, files, audio, title}。"""
    s = store.get_session(conn, sid)
    if not s:
        raise ValueError("场次不存在")
    out = Path(out_dir) if out_dir else (SHARE_DIR / sid)
    out.mkdir(parents=True, exist_ok=True)

    summary = export.to_markdown(conn, sid)
    srt = export.to_srt(conn, sid)
    (out / "summary.md").write_text(summary, encoding="utf-8")
    (out / "transcript.srt").write_text(srt, encoding="utf-8")

    audio_name = ""
    if include_audio:
        ap = Path(str(s.get("audio_path") or ""))
        if ap.is_file() and ap.stat().st_size > 0:
            # WAV 还没转码时顺手转一份 m4a：包小一个数量级，妙记也更愿意收。
            if ap.suffix.lower() == ".wav":
                m4a = audio.transcode_session(sid, ap, conn=conn)
                if m4a and m4a.is_file():
                    ap = m4a
            dst = out / ("audio" + ap.suffix.lower())
            if not dst.exists() or dst.stat().st_mtime < ap.stat().st_mtime:
                shutil.copyfile(ap, dst)
            audio_name = dst.name

    (out / "README.md").write_text(
        _readme(s, audio_name, str(s.get("minutes_url") or "")), encoding="utf-8")
    meta = {
        "session_id": sid, "title": s.get("title") or "",
        "started_at": s.get("started_at") or "",
        "duration_s": float(s.get("duration_s") or 0),
        "audio": audio_name, "minutes_url": s.get("minutes_url") or "",
        "built_at": store.now(),
    }
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    files = sorted(p.name for p in out.iterdir() if p.is_file())
    return {"dir": out, "files": files, "audio": audio_name,
            "title": s.get("title") or sid}


def zip_package(folder: Path | str) -> Path:
    """把目录打成 zip。音频已经压缩过，ZIP_STORED 更快也不明显变大。"""
    folder = Path(folder)
    zpath = folder.with_suffix(".zip")
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
        for p in sorted(folder.iterdir()):
            if p.is_file():
                z.write(p, arcname=p.name)
    tmp.replace(zpath)
    return zpath


def cleanup(days: int = 7) -> int:
    """清掉过期的分享包目录/zip，避免 data/share 无限增长。"""
    if not SHARE_DIR.is_dir():
        return 0
    cutoff = time.time() - max(1, int(days)) * 86400
    n = 0
    for p in SHARE_DIR.iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n
