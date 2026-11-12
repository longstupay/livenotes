# -*- coding: utf-8 -*-
"""录音归档：WAV -> m4a，飞书片段 opus，原文件保留与清理。

为什么不是「收尾直接写压缩格式」：ASR/VAD 要的是 16k 单声道 PCM，采集链路
不能为了省磁盘去改采样率或只存人声段——那会破坏时间轴，也让「当时确实安静」
不再是可核对的证据。所以策略是：采集期照旧落 PCM/WAV，收尾后异步转一份
m4a 作为长期可播的归档，原 WAV 按保留期删除。

m4a（AAC-LC）而不是 opus 作为归档：Chrome/Edge/Safari/飞书 WebView 都能直接
播，飞书妙记也吃常见音视频格式；opus 只在「飞书语音片段」这种短片段上按需生成。
32 kbps 单声道对语音足够，13 小时一场约 187 MB（WAV 是 1.49 GB）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
import wave
from pathlib import Path

from . import logs, store
from .config import (AUDIO_AAC_BITRATE, AUDIO_OPUS_BITRATE, AUDIO_RETENTION_DAYS,
                     CLIPS_DIR, SAMPLE_RATE, WAV_RETENTION_DAYS)

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_FFMPEG: str | None | bool = False      # False = 还没探测过
_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()

AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
}


def ffmpeg_path() -> str | None:
    """优先 LIVENOTES_FFMPEG，其次 PATH。探测结果缓存，避免每场都查一次。"""
    global _FFMPEG
    if _FFMPEG is not False:
        return _FFMPEG or None
    cand = (os.environ.get("LIVENOTES_FFMPEG") or "").strip() or shutil.which("ffmpeg")
    _FFMPEG = cand or ""
    return cand or None


def content_type(path: Path | str) -> str:
    return AUDIO_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def is_audio(path: Path | str) -> bool:
    return Path(path).suffix.lower() in AUDIO_TYPES


def _run(args: list[str], timeout: float = 1800.0) -> tuple[bool, str]:
    ff = ffmpeg_path()
    if not ff:
        return False, "ffmpeg 不在 PATH（或 LIVENOTES_FFMPEG 没配）"
    try:
        p = subprocess.run([ff] + args, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=timeout,
                           creationflags=_CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "转码超时"
    except OSError as e:
        return False, str(e)
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    return p.returncode == 0, err[-600:]


def _input_args(src: Path) -> list[str]:
    if src.suffix.lower() == ".pcm":
        return ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1"]
    return []


def to_m4a(src: Path | str, dst: Path | str, bitrate: str | None = None) -> tuple[bool, str]:
    """转 AAC-LC 单声道 m4a。保留 +faststart，网页拖动时不用等整段下载。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = (["-hide_banner", "-loglevel", "error", "-y"] + _input_args(src)
            + ["-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-c:a", "aac", "-b:a", bitrate or AUDIO_AAC_BITRATE,
               "-movflags", "+faststart", str(dst)])
    return _run(args)


def to_opus(src: Path | str, dst: Path | str, bitrate: str | None = None) -> tuple[bool, str]:
    """转 Ogg Opus，给飞书语音消息用。短片段才走这里，不做整场归档。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    args = (["-hide_banner", "-loglevel", "error", "-y"] + _input_args(src)
            + ["-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
               "-c:a", "libopus", "-b:a", bitrate or AUDIO_OPUS_BITRATE,
               "-vbr", "on", "-application", "voip", str(dst)])
    return _run(args)


# ---------------------------------------------------------------- 归档

def transcode_session(sid: str, wav_path: Path | str | None = None,
                      conn=None) -> Path | None:
    """把一场的 WAV/PCM 转成 m4a，成功后更新 sessions.audio_path。

    失败不改数据库：WAV 仍然可播，只是没省下空间。调用方（收尾线程）不能
    因为转码失败把「录音落盘」算失败。
    """
    own = conn is None
    conn = conn or store.connect()
    try:
        s = store.get_session(conn, sid)
        if not s:
            return None
        src = Path(str(wav_path or s.get("audio_original_path")
                       or s.get("audio_path") or ""))
        if not src.is_file() or src.stat().st_size <= 0:
            return None
        if src.suffix.lower() not in (".wav", ".pcm"):
            return None
        dst = src.parent / (sid + ".m4a")
        if dst.is_file() and dst.stat().st_size > 0 and dst.stat().st_mtime >= src.stat().st_mtime:
            # 已经转过了（服务重启/重复收尾），不重复跑 ffmpeg
            _mark(conn, sid, dst, src)
            return dst
        ok, err = to_m4a(src, dst)
        if not ok or not dst.is_file() or dst.stat().st_size <= 0:
            logs.log_event("audio.transcode_failed", level="warn", session_id=sid,
                           src=str(src), error=err)
            return None
        _mark(conn, sid, dst, src)
        logs.log_event("audio.transcoded", session_id=sid, src=str(src),
                       dst=str(dst), bytes=dst.stat().st_size)
        return dst
    except Exception:
        traceback.print_exc()
        return None
    finally:
        if own:
            conn.close()


def _mark(conn, sid: str, dst: Path, src: Path) -> None:
    store.set_audio_meta(conn, sid, audio_path=str(dst), audio_format="m4a",
                         audio_bytes=dst.stat().st_size,
                         audio_transcoded_at=store.now(),
                         audio_original_path=str(src))


def schedule_transcode(sid: str, wav_path: Path | str | None = None) -> None:
    """异步转码。同一场只允许一个线程，收尾/重启重复调用不会叠加。"""
    sid = str(sid or "")
    if not sid:
        return
    with _LOCK:
        if sid in _INFLIGHT:
            return
        _INFLIGHT.add(sid)

    def run() -> None:
        try:
            transcode_session(sid, wav_path)
        except Exception:
            traceback.print_exc()
        finally:
            with _LOCK:
                _INFLIGHT.discard(sid)

    threading.Thread(target=run, name="audio-transcode-" + sid, daemon=True).start()


def cleanup_originals(conn=None, days: int | None = None) -> int:
    """删除已转码的原始 WAV。只删「当前可播文件不是它自己」的场次。"""
    days = WAV_RETENTION_DAYS if days is None else int(days)
    if days < 0:
        return 0
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        cutoff = time.time() - max(0, days) * 86400
        rows = conn.execute(
            "SELECT id, audio_path, audio_original_path FROM sessions "
            "WHERE audio_original_path IS NOT NULL AND audio_original_path <> ''"
        ).fetchall()
        for r in rows:
            orig = Path(str(r["audio_original_path"]))
            cur = Path(str(r["audio_path"] or ""))
            if not orig.is_file():
                store.set_audio_meta(conn, r["id"], audio_original_path=None)
                continue
            try:
                same = cur and cur.is_file() and cur.resolve() == orig.resolve()
                if same or not cur.is_file():
                    continue
                if orig.stat().st_mtime > cutoff:
                    continue
                orig.unlink(missing_ok=True)
                store.set_audio_meta(conn, r["id"], audio_original_path=None)
                n += 1
                logs.log_event("audio.original_removed", session_id=r["id"],
                               path=str(orig))
            except OSError:
                traceback.print_exc()
        return n
    finally:
        if own:
            conn.close()


def cleanup_expired(conn=None, days: int | None = None) -> int:
    """按保留期删除归档音频。默认关闭（0 = 永久保留）。"""
    days = AUDIO_RETENTION_DAYS if days is None else int(days)
    if days <= 0:
        return 0
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        cutoff = time.time() - days * 86400
        rows = conn.execute(
            "SELECT id, audio_path, audio_original_path FROM sessions "
            "WHERE audio_path IS NOT NULL AND audio_path <> ''"
        ).fetchall()
        for r in rows:
            p = Path(str(r["audio_path"]))
            try:
                if not p.is_file() or p.stat().st_mtime > cutoff:
                    continue
                p.unlink(missing_ok=True)
                orig = Path(str(r["audio_original_path"] or ""))
                if orig and orig != p:
                    orig.unlink(missing_ok=True)
                store.drop_audio(conn, r["id"])
                n += 1
                logs.log_event("audio.expired", session_id=r["id"], path=str(p))
            except OSError:
                traceback.print_exc()
        return n
    finally:
        if own:
            conn.close()


def backfill_wavs(conn=None, limit: int = 0) -> int:
    """把历史 WAV 逐场转成 m4a。顺序执行，避免和采集抢 CPU/内存。

    老库没有 audio_original_path，转码成功后由 _mark 补上；这样保留期清理
    才有依据。limit=0 表示不限场次（CLI/启动补转用）。
    """
    own = conn is None
    conn = conn or store.connect()
    n = 0
    try:
        sql = ("SELECT id, audio_path FROM sessions "
               "WHERE audio_path LIKE '%.wav' "
               "AND (audio_format IS NULL OR audio_format = 'wav') "
               "ORDER BY started_at ASC")
        if limit and limit > 0:
            sql += " LIMIT %d" % int(limit)
        rows = conn.execute(sql).fetchall()
        for r in rows:
            if transcode_session(r["id"], r["audio_path"], conn=conn):
                n += 1
        return n
    finally:
        if own:
            conn.close()


def start_backfill() -> threading.Thread:
    """启动后台补转历史 WAV；只跑一次，失败不影响服务。"""
    def run() -> None:
        try:
            n = backfill_wavs()
            if n:
                logs.log_event("audio.backfill", count=n)
        except Exception:
            traceback.print_exc()

    t = threading.Thread(target=run, name="audio-backfill", daemon=True)
    t.start()
    return t


def cleanup_clips(max_age_s: float = 86400.0) -> int:
    """片段是缓存，不是资产：超过一天就删，避免 data/clips 无限增长。"""
    if not CLIPS_DIR.is_dir():
        return 0
    cutoff = time.time() - max(60.0, float(max_age_s))
    n = 0
    for p in CLIPS_DIR.glob("*.opus"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    return n


def start_sweeper(interval_s: float = 6 * 3600) -> threading.Thread:
    """定期清理原 WAV / 过期归档 / 片段缓存 / 分享包 / 报告。启动时也跑一次。"""
    def loop() -> None:
        while True:
            try:
                removed = cleanup_originals()
                expired = cleanup_expired()
                clips = cleanup_clips()
                # share/report 都依赖 audio（模块级），只能在这里延迟导入。
                from . import report as _report, share as _share
                packages = _share.cleanup()
                reports = _report.cleanup()
                if removed or expired or clips or packages or reports:
                    logs.log_event("audio.sweep", originals=removed,
                                   expired=expired, clips=clips,
                                   packages=packages, reports=reports)
            except Exception:
                traceback.print_exc()
            time.sleep(max(300.0, float(interval_s)))

    t = threading.Thread(target=loop, name="audio-sweeper", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------- 切片

def _slice_wav(src: Path, dst: Path, start: float, duration: float) -> bool:
    try:
        with wave.open(str(src), "rb") as w:
            rate = w.getframerate()
            a = int(max(0.0, start) * rate)
            b = a + int(max(0.2, duration) * rate)
            w.setpos(min(a, w.getnframes()))
            data = w.readframes(max(0, min(b, w.getnframes()) - a))
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(data)
        return bool(data)
    except (OSError, EOFError, wave.Error):
        traceback.print_exc()
        return False


def _slice_pcm(src: Path, dst: Path, start: float, duration: float) -> bool:
    """spool 是 16k/单声道/s16le：按样本数切，比 ffmpeg seek 更准也更快。"""
    try:
        offset = int(max(0.0, start) * SAMPLE_RATE) * 2
        nbytes = int(max(0.2, duration) * SAMPLE_RATE) * 2
        with open(src, "rb") as f:
            f.seek(offset)
            data = f.read(nbytes)
        if not data:
            return False
        with wave.open(str(dst), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(data)
        return True
    except OSError:
        traceback.print_exc()
        return False


def extract_opus(src: Path | str, dst: Path | str, start: float,
                 duration: float) -> tuple[bool, str]:
    """从 WAV/PCM/m4a/opus 切一段，输出飞书语音用的 opus。"""
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.2, float(duration))
    if src.suffix.lower() in (".wav", ".pcm"):
        with tempfile.TemporaryDirectory(prefix="livenotes-clip-") as td:
            tmp = Path(td) / "slice.wav"
            ok = (_slice_pcm(src, tmp, start, duration) if src.suffix.lower() == ".pcm"
                  else _slice_wav(src, tmp, start, duration))
            if not ok:
                return False, "这一段音频读不出来（可能已经写完了或文件被清理）"
            return to_opus(tmp, dst)
    args = (["-hide_banner", "-loglevel", "error", "-y",
             "-ss", "%.3f" % max(0.0, float(start)),
             "-t", "%.3f" % duration, "-i", str(src), "-vn",
             "-ac", "1", "-ar", str(SAMPLE_RATE),
             "-c:a", "libopus", "-b:a", AUDIO_OPUS_BITRATE,
             "-vbr", "on", "-application", "voip", str(dst)])
    return _run(args, timeout=120.0)


def slice_wav_bytes(path: Path | str, t0: float, t1: float) -> bytes | None:
    """给 /api/audio?t_start=&t_end= 的兼容出口：非 WAV 文件用 ffmpeg 现切。"""
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            import io
            with wave.open(str(path), "rb") as w:
                rate, ch, sw, n = (w.getframerate(), w.getnchannels(),
                                   w.getsampwidth(), w.getnframes())
                a = min(n, int(max(0, t0) * rate))
                b = min(n, int(max(t0 + 0.2, t1) * rate))
                w.setpos(a)
                raw = w.readframes(max(0, b - a))
            buf = io.BytesIO()
            with wave.open(buf, "wb") as out:
                out.setnchannels(ch); out.setsampwidth(sw); out.setframerate(rate)
                out.writeframes(raw)
            return buf.getvalue()
        except (OSError, EOFError, wave.Error):
            return None
    ff = ffmpeg_path()
    if not ff:
        return None
    # 输出到临时文件而不是 pipe：WAV 头里的长度需要回写，pipe 上只能写
    # 0xFFFFFFFF，浏览器拿到后读不出时长。
    fd, tmp_name = tempfile.mkstemp(suffix=".wav", prefix="slice_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    args = [ff, "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "%.3f" % max(0.0, float(t0)),
            "-t", "%.3f" % max(0.2, float(t1) - float(t0)),
            "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE),
            "-acodec", "pcm_s16le", str(tmp_path)]
    try:
        p = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=300, creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        tmp_path.unlink(missing_ok=True)
        return None
    if p.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        return None
    try:
        data = tmp_path.read_bytes()
    except OSError:
        data = b""
    finally:
        tmp_path.unlink(missing_ok=True)
    return data or None
