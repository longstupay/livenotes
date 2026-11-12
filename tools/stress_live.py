# -*- coding: utf-8 -*-
"""实时链路的并发压测：1/3/5/10 路同时推流，输出 RTF、P50/P95 延迟、丢块率、CPU/内存。

    # 快速验收（默认每档 60 秒，预热 10 秒）
    python tools/stress_live.py --tracks 1,3,5,10 --duration 60

    # 正式 2 小时长稳（4 档共约 8 小时）
    python tools/stress_live.py --tracks 1,3,5,10 --duration 7200 --warmup 60 \
        --json data/asr-soak-2h.json

指标口径（与线上 /api/live/status 的 asr 字段一致）：

- RTF = ASR 解码耗时 / 送进解码器的音频秒数（含定稿 + 临时字幕）
- 定稿 P50/P95 = 这句话最后一个采样到达浏览器服务端 -> 定稿落库上屏的墙钟时间，
  包含 VAD 等静音/等 4 秒强切、排队、解码、声纹、写库
- 临时 P50/P95 = 临时字幕音频到达 -> 上屏（排队 + 解码）
- 定稿丢句率 = 被工作池丢掉的定稿 / 提交的定稿
- PCM 丢块率 = 浏览器块进入会话队列时被丢掉的块 / 总块数
- CPU = 进程内所有线程的 process_time 增量 / 墙钟（即占用的核数）
- RSS = 进程 Working Set 峰值

音频默认取仓库 data/audio 下最长的一段 16k 单声道 WAV，各路线从不同位置开始循环播放。
压测数据写到临时目录，不污染真实场次；加 --keep-data 可保留现场。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHUNK_BYTES = 8192          # 4096 采样 = 256ms，与浏览器端一致
SAMPLE_RATE = 16000


# ---------------------------------------------------------------- 系统指标
class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except Exception:
        pass
    if os.name != "nt":
        return None
    try:
        k32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        p = _PMC()
        p.cb = ctypes.sizeof(_PMC)
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(p), p.cb):
            return round(p.WorkingSetSize / 1e6, 1)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 参数/路径
def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="LiveNotes 实时链路并发压测")
    ap.add_argument("--tracks", default="1,3,5,10", help="路数，逗号分隔")
    ap.add_argument("--duration", type=float, default=60.0, help="每档测量秒数")
    ap.add_argument("--warmup", type=float, default=10.0, help="每档预热秒数（不计入指标）")
    ap.add_argument("--audio", default="", help="16k 单声道 16bit WAV；默认自动找最长的")
    ap.add_argument("--models", default="", help="模型目录（默认 ./models 或主工作区）")
    ap.add_argument("--workers", type=int, default=0, help="ASR worker 数，0=自动")
    ap.add_argument("--threads", type=int, default=0, help="单次解码 intra-op 线程，0=自动")
    ap.add_argument("--json", default="", help="指标 JSON 输出路径")
    ap.add_argument("--md", default="", help="Markdown 报告输出路径")
    ap.add_argument("--keep-data", action="store_true", help="保留压测产生的临时数据目录")
    return ap.parse_args()


def _candidate_dirs(name: str) -> list[Path]:
    """本工作区 + 主工作区（worktree 的代码在 .claude/worktrees/<name> 下）。"""
    dirs = [ROOT / name]
    try:
        dirs.append(ROOT.parents[2] / name)
    except IndexError:
        pass
    return dirs


def _find_audio(explicit: str) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise SystemExit("找不到音频：%s" % p)
    files: list[Path] = []
    for d in _candidate_dirs("data/audio"):
        if d.is_dir():
            files.extend(d.glob("*.wav"))
    files = [p for p in files if p.stat().st_size > 32000]
    if not files:
        raise SystemExit("没有可用的 WAV。请用 --audio 指定一段 16k 单声道录音"
                         "（可先跑一次实时监听产生 data/audio/*.wav）。")
    return max(files, key=lambda p: p.stat().st_size)


def _find_models(explicit: str) -> Path:
    if explicit:
        return Path(explicit)
    for d in _candidate_dirs("models"):
        if (d / "sensevoice" / "model.int8.onnx").is_file():
            return d
    return ROOT / "models"


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit("压测音频需要 16k 单声道 16bit：%s" % path)
        if w.getframerate() != SAMPLE_RATE:
            raise SystemExit("压测音频采样率需要 16000：%s" % path)
        return w.readframes(w.getnframes())


# ---------------------------------------------------------------- 推流
class _FeedCounter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fed = 0
        self.errors = 0

    def add(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.fed += 1
            else:
                self.errors += 1


def _feeder(live, sid: str, pcm: bytes, offset: int, stop_evt: threading.Event,
            counter: _FeedCounter) -> None:
    period = CHUNK_BYTES / (SAMPLE_RATE * 2.0)
    pos = offset % len(pcm)
    next_t = time.monotonic()
    while not stop_evt.is_set():
        end = pos + CHUNK_BYTES
        if end <= len(pcm):
            chunk, pos = pcm[pos:end], end
        else:
            n = len(pcm) - pos
            chunk = pcm[pos:] + pcm[: CHUNK_BYTES - n]
            pos = CHUNK_BYTES - n
        try:
            live.feed(sid, chunk)
            counter.add(True)
        except Exception:
            counter.add(False)
        next_t += period
        delay = next_t - time.monotonic()
        if delay > 0:
            stop_evt.wait(delay)
        elif delay < -1.0:
            next_t = time.monotonic()      # 落后超过 1 秒就不再无限追赶


# ---------------------------------------------------------------- 单档压测
def _run_level(live, pool, tracks: int, audio: bytes, duration: float,
               warmup: float) -> dict:
    print("\n[%d 路] 启动会话…" % tracks, flush=True)
    sessions: list[str] = []
    for i in range(tracks):
        sessions.append(live.start("压测 %d 路 #%d" % (tracks, i + 1))["session_id"])
    print("  会话：%s" % ", ".join(sessions), flush=True)

    stop_evt = threading.Event()
    counters = [_FeedCounter() for _ in range(tracks)]
    threads: list[threading.Thread] = []
    for i, sid in enumerate(sessions):
        offset = (i * 7919 * CHUNK_BYTES) % len(audio)
        t = threading.Thread(target=_feeder, args=(live, sid, audio, offset, stop_evt, counters[i]),
                             name="feed-%d" % i, daemon=True)
        t.start()
        threads.append(t)

    if warmup > 0:
        print("  预热 %.0fs…" % warmup, flush=True)
        time.sleep(warmup)

    # 预热结束：清空统计，只统计稳定态
    pool.reset_stats()
    base = {sid: live.status(sid) for sid in sessions}
    for c in counters:
        with c.lock:
            c.fed = 0
            c.errors = 0

    cpu_last = time.process_time()
    wall_last = time.monotonic()
    rss_peak = _rss_mb() or 0.0
    samples: list[dict] = []
    measure_start = wall_last
    deadline = measure_start + duration
    print("  测量 %.0fs…" % duration, flush=True)
    next_sample = measure_start
    while time.monotonic() < deadline:
        time.sleep(max(0.05, min(1.0, deadline - time.monotonic())))
        now = time.monotonic()
        if now - next_sample < 1.0 and now < deadline:
            continue
        next_sample = now
        cpu_now = time.process_time()
        wall_now = time.monotonic()
        cores = (cpu_now - cpu_last) / max(1e-6, wall_now - wall_last)
        rss = _rss_mb()
        if rss:
            rss_peak = max(rss_peak, rss)
        st = pool.stats()
        samples.append({
            "t": round(wall_now - measure_start, 1),
            "cpu_cores": round(cores, 2),
            "rss_mb": rss,
            "rtf": st["rtf"],
            "pending_finals": st["pending_finals"],
            "pending_partials": st["pending_partials"],
            "final_p95": st["final_latency"]["p95"],
        })
        cpu_last, wall_last = cpu_now, wall_now

    stop_evt.set()
    for t in threads:
        t.join(timeout=5)
    feed_wall = max(0.001, time.monotonic() - measure_start)

    print("  收尾（drain）…", flush=True)
    stopped = [live.stop(sid, finalize=False) for sid in sessions]
    final = pool.stats()

    # 单档汇总
    base_n = sum(int(b["n_utterances"]) for b in base.values())
    base_dropped = sum(int(b["dropped"]) for b in base.values())
    base_recog = sum(float(b["recognized_s"]) for b in base.values())
    base_elapsed = sum(float(b["elapsed"]) for b in base.values())
    n_utts = sum(int(o["n_utterances"]) for o in stopped) - base_n
    dropped_pcm = sum(int(o["dropped"]) for o in stopped) - base_dropped
    fed = sum(c.fed for c in counters)
    errors = sum(c.errors for c in counters)
    recognized = sum(float(o["recognized_s"]) for o in stopped) - base_recog
    elapsed = sum(float(o["elapsed"]) for o in stopped) - base_elapsed

    cpu_samples = [s["cpu_cores"] for s in samples]
    rss_values = [s["rss_mb"] for s in samples if s["rss_mb"]]
    if samples:
        cpu_avg = round(sum(cpu_samples) / len(cpu_samples), 2)
        cpu_max = round(max(cpu_samples), 2)
    else:
        cpu_avg = cpu_max = 0.0
    result = {
        "tracks": tracks,
        "duration_s": round(feed_wall, 1),
        "audio_fed_s": round(fed * CHUNK_BYTES / (SAMPLE_RATE * 2.0), 1),
        "chunks_fed": fed,
        "feed_errors": errors,
        "n_utterances": n_utts,
        "rtf": final["rtf"],
        "decode_s": final["decode_s"],
        "audio_decoded_s": final["audio_s"],
        "decoded_finals": final["decoded_finals"],
        "decoded_partials": final["decoded_partials"],
        "submitted_finals": final["submitted_finals"],
        "dropped_finals": final["dropped_finals"],
        "final_drop_rate": round(final["dropped_finals"] / max(1, final["submitted_finals"]), 4),
        "superseded_partials": final["superseded_partials"],
        "expired_partials": final["expired_partials"],
        "stale_partials": final["stale_partials"],
        "asr_errors": final["errors"],
        "final_latency": final["final_latency"],
        "partial_latency": final["partial_latency"],
        "final_wait": final["final_wait"],
        "partial_wait": final["partial_wait"],
        "dropped_pcm_chunks": dropped_pcm,
        "pcm_drop_rate": round(dropped_pcm / max(1, fed + dropped_pcm), 5),
        "untranscribed_s": round(max(0.0, elapsed - recognized), 1),
        "cpu_avg_cores": cpu_avg,
        "cpu_max_cores": cpu_max,
        "rss_peak_mb": round(rss_peak, 1),
        "rss_avg_mb": round(sum(rss_values) / len(rss_values), 1) if rss_values else None,
        "samples": samples,
    }
    print("  完成：定稿 %d 句 · RTF %.3f · 定稿 P95 %ss · CPU 均 %.1f 核 · RSS 峰 %.0fMB"
          % (n_utts, result["rtf"],
             result["final_latency"]["p95"], result["cpu_avg_cores"], result["rss_peak_mb"]),
          flush=True)
    return result


# ---------------------------------------------------------------- 报告
def _fmt(v, suffix: str = "") -> str:
    return "-" if v is None else ("%s%s" % (round(v, 3), suffix))


def _markdown(meta: dict, results: list[dict]) -> str:
    lines = [
        "# 实时链路并发压测报告",
        "",
        "- 时间：%s" % meta["started_at"],
        "- 机器：%d 核 · %s" % (meta["cpu_count"], meta["platform"]),
        "- 配置：ASR worker %d 个 · 单次解码 intra-op %d 线程 · 每档测量 %.0fs（预热 %.0fs）"
        % (meta["workers"], meta["threads"], meta["duration"], meta["warmup"]),
        "- 音频：%s（16k 单声道）" % meta["audio"],
        "",
        "| 路数 | RTF | 定稿 P50 | 定稿 P95 | 临时 P95 | 定稿排队 P95 | 定稿丢句率 | PCM 丢块率 | CPU 均/峰 | RSS 峰 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    single_p95 = None
    for r in results:
        if r["tracks"] == 1:
            single_p95 = r["final_latency"]["p95"]
    for r in results:
        lines.append("| %d | %.3f | %s | %s | %s | %s | %.2f%% | %.2f%% | %.1f / %.1f 核 | %.0f MB |"
                     % (r["tracks"], r["rtf"],
                        _fmt(r["final_latency"]["p50"], "s"),
                        _fmt(r["final_latency"]["p95"], "s"),
                        _fmt(r["partial_latency"]["p95"], "s"),
                        _fmt(r["final_wait"]["p95"], "s"),
                        r["final_drop_rate"] * 100, r["pcm_drop_rate"] * 100,
                        r["cpu_avg_cores"], r["cpu_max_cores"], r["rss_peak_mb"]))
    lines += ["", "## 验收口径", "",
              "- 定稿延迟从「这句话最后一个采样到达服务端」算起，包含 VAD 等静音/等 4 秒强切、"
              "排队、解码、声纹、写库；不是只算 decode 时间。",
              "- RTF 只统计真正送进解码器的音频（定稿 + 临时字幕），不含 VAD 丢弃的静音。",
              "- PCM 丢块率统计浏览器 256ms 块在会话队列溢出时被丢的比例，正常应为 0。",
              ""]
    if single_p95:
        lines.append("## 与单路的 P95 对比（验收要求：10 路 P95 ≤ 单路 2 倍）")
        lines.append("")
        lines.append("| 路数 | 定稿 P95 | 相对单路 | 结论 |")
        lines.append("|---:|---:|---:|---|")
        for r in results:
            p95 = r["final_latency"]["p95"]
            ratio = (p95 / single_p95) if p95 else None
            ok = "通过" if ratio is not None and ratio <= 2.0 else "未通过"
            lines.append("| %d | %s | %s | %s |"
                         % (r["tracks"], _fmt(p95, "s"),
                            "-" if ratio is None else "%.2fx" % ratio,
                            "-" if r["tracks"] == 1 else ok))
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    tracks = [int(x) for x in args.tracks.split(",") if x.strip()]
    if not tracks:
        raise SystemExit("--tracks 为空")
    audio_path = _find_audio(args.audio)
    models = _find_models(args.models)
    audio = _read_wav(audio_path)

    data_dir = Path(tempfile.mkdtemp(prefix="livenotes-stress-"))
    os.environ["LIVENOTES_DATA_DIR"] = str(data_dir)
    os.environ["LIVENOTES_MODELS_DIR"] = str(models)
    if args.workers > 0:
        os.environ["LIVENOTES_ASR_WORKERS"] = str(args.workers)
    if args.threads > 0:
        os.environ["LIVENOTES_ASR_THREADS"] = str(args.threads)

    sys.path.insert(0, str(ROOT))
    from app import live, store
    from app.config import asr_threads, asr_workers

    POOL = live.POOL

    conn = store.connect()
    store.init_db(conn)
    conn.close()

    meta = {
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_count": os.cpu_count(),
        "platform": sys.platform,
        "workers": asr_workers(),
        "threads": asr_threads(),
        "duration": args.duration,
        "warmup": args.warmup,
        "audio": str(audio_path),
        "data_dir": str(data_dir),
    }
    print("模型：%s" % models)
    print("音频：%s（%.1fs）" % (audio_path, len(audio) / (SAMPLE_RATE * 2)))
    print("数据：%s" % data_dir)
    print("工作池：%d 个 worker · 单次解码 %d 线程" % (meta["workers"], meta["threads"]))

    results: list[dict] = []
    started = time.monotonic()
    try:
        for n in tracks:
            results.append(_run_level(live, POOL, n, audio, args.duration, args.warmup))
    finally:
        live.shutdown()

    total = time.monotonic() - started
    print("\n总耗时 %.0fs" % total)
    md = _markdown(meta, results)
    if args.md:
        out = Path(args.md)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md + "\n", encoding="utf-8")
        print("报告：%s" % out)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"meta": meta, "levels": results},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        print("指标：%s" % out)
    print("\n" + md)

    keep = args.keep_data or bool(args.json and Path(args.json).parent == data_dir)
    if not keep:
        import shutil
        shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
