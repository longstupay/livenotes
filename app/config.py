# -*- coding: utf-8 -*-
"""全局配置。路径全部基于项目根目录。"""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
WEB_DIR = APP_DIR / "web"
# 压测/自测可以把运行数据放到临时目录，避免污染真实场次
DATA_DIR = Path(os.environ.get("LIVENOTES_DATA_DIR", str(ROOT / "data")))
AUDIO_DIR = DATA_DIR / "audio"
DB_PATH = DATA_DIR / "livenotes.db"

HOST = os.environ.get("LIVENOTES_HOST", "127.0.0.1")
PORT = int(os.environ.get("LIVENOTES_PORT", "8765"))
# 服务重启后要不要自动接着采上次没结束的房间。默认关：进程退出时残留的
# pending/starting/live 只是「上次进程还活着」的化石状态，自动拉起会在用户
# 不知情时重开直播间。无人值守的机器可以显式设 1。
RESUME_ROOMS = os.environ.get("LIVENOTES_RESUME_ROOMS", "0").lower() in ("1", "true", "yes")
def _lan_ip() -> str:
    """取本机局域网 IP。手机点卡片链接时不能用 127.0.0.1，这里自动填对。

    不能用 "连 8.8.8.8 看出口网卡" 那招：装了代理/WSL 的机器会选中
    198.18.x 或 172.17.x 这类虚拟网卡，手机根本连不上。改成枚举后按网段择优。
    """
    import socket
    cands: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip.startswith(("127.", "169.254.", "198.18.", "198.19.")):
                continue
            if ip not in cands:
                cands.append(ip)
    except OSError:
        pass

    def rank(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0                      # 家用路由器最常见的网段，优先
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                if 16 <= int(ip.split(".")[1]) <= 31:
                    return 2
            except ValueError:
                pass
        return 3

    return sorted(cands, key=rank)[0] if cands else HOST


# 推给手机时用的地址：手机访问不到 127.0.0.1，未显式配置时自动用局域网 IP
BASE_URL = os.environ.get("LIVENOTES_BASE_URL") or (
    "http://%s:%d" % (HOST if HOST not in ("127.0.0.1", "0.0.0.0") else _lan_ip(), PORT))
# 高光推送：默认每天最多 5 条。单用户场景下，"烦"比"漏"代价大得多，
# 但 3 条 + 30 分钟冷却叠起来，一场两小时的直播最多只响一次——用户会以为没在推。
PUSH_DAILY_CAP = int(os.environ.get("LIVENOTES_PUSH_CAP", "5"))
PUSH_ENABLED = os.environ.get("LIVENOTES_PUSH", "1").lower() not in ("0", "false", "no")
# 同一条直播里，两条实时推送之间至少隔多少秒（按录音内时间算）。
# 旧默认 1800 太保守：一句话值不值得看，40 秒后就没意义了，而半小时的间隔
# 让它只能"事后补发"。10 分钟是打扰与时效的折中。
PUSH_COOLDOWN_S = float(os.environ.get("LIVENOTES_PUSH_COOLDOWN", "600"))
# 单场实时推送上限：宁可让回顾补齐，也别让一场 3 小时的直播响 20 次。
PUSH_SESSION_CAP = int(os.environ.get("LIVENOTES_PUSH_SESSION_CAP", "3"))
# 结束时的「本场高光回顾」：实时推送没覆盖到的，在这一条里补齐。
# 关掉后，结束只补推最后一条压着没发的，不发回顾。
PUSH_DIGEST = os.environ.get("LIVENOTES_PUSH_DIGEST", "1").lower() not in ("0", "false", "no")
# 卡片 vs 纯文本：卡片把时间做成字段、回听做成按钮，手机上少一次点按；
# 但卡片 schema 随飞书版本变，所以失败必须回退纯文本（见 live._send）。
PUSH_CARD = os.environ.get("LIVENOTES_PUSH_CARD", "1").lower() not in ("0", "false", "no")

# 实时监听用的本地模型（不装也能用演示/回放，只是「实时监听」不可用）
MODELS_DIR = Path(os.environ.get("LIVENOTES_MODELS_DIR", str(ROOT / "models")))
SENSEVOICE_MODEL = MODELS_DIR / "sensevoice" / "model.int8.onnx"
SENSEVOICE_TOKENS = MODELS_DIR / "sensevoice" / "tokens.txt"
VAD_MODEL = MODELS_DIR / "vad" / "silero_vad.onnx"
SPEAKER_MODEL = MODELS_DIR / "speaker" / "campplus.onnx"

LIVE_ASR_LANGUAGE = os.environ.get("LIVENOTES_ASR_LANGUAGE", "zh")
# 在线聚类的上限。原来的 3 太紧：第 4 个人一出现就被静默并进最近的那个簇，
# 整场再也纠不回来。现在放宽到 8，并在结束后用全场声纹重聚类（app/recluster.py）
# 把结果覆盖掉，所以这个数字只影响"实时阶段先怎么显示"。
LIVE_MAX_SPEAKERS = int(os.environ.get("LIVENOTES_MAX_SPEAKERS", "8"))
LIVE_MIN_SPEECH_S = 0.3          # 短于这个时长的人声不送 ASR
LIVE_MAX_SEGMENT_S = 20          # VAD 自带的软上限
LIVE_FORCE_SEGMENT_S = float(os.environ.get("LIVENOTES_FORCE_SEGMENT_S", "4"))
# ↑ 直播 BGM 会让 VAD 一直判"在说话"，max_speech_duration 在长语音里不保证按时切；
#   到点强制 flush，保证每 4 秒左右必出一句，延迟有上界。

# 滚动字幕：当前这句还没切完时，每隔 1 秒把已收到的音频先识别一次，作灰色临时字幕。
# 设为 0 可关闭（关闭后退回"4 秒一句"的定稿模式）。
LIVE_PARTIAL_INTERVAL_S = float(os.environ.get("LIVENOTES_PARTIAL_INTERVAL_S", "1.0"))
LIVE_PARTIAL_MIN_S = 0.8      # 太短不识别，避免只有一个字的闪烁
LIVE_PARTIAL_MAX_S = 8.0      # 临时字幕最多看最近 8 秒，防止长句越算越慢
# 断流看门狗：采集页每 256ms 推一块 PCM（安静也推），所以「超过这个秒数一块都没到」
# 就不是安静，而是链路断了——浏览器崩了、页面被关、页面被冻住、采集页卡死。
# 服务端按这个阈值自动收尾（见 server._watch_stalled）：落 WAV、排队总结、发回顾，
# 否则整夜挂着一个 running 的场次，看门狗不跑就只能等进程重启才捞得回来。
# 设 0 关闭。
LIVE_STALL_STOP_S = float(os.environ.get("LIVENOTES_STALL_SECONDS", "120"))
# 断流预警：块停了先发一条飞书，不结束场次。30 秒足够把「浏览器崩了/页面被冻住」
# 和「主播只是安静」区分开——安静时块照推，所以「没块」一定是采集链路出了问题。
# 设 0 关闭。
LIVE_STALL_WARN_S = float(os.environ.get("LIVENOTES_STALL_WARN_SECONDS", "30"))

# ASR 工作池：采集/VAD 与识别解耦，多个会话共享一个识别器（sherpa-onnx 的
# Recognizer 在 CPU provider 下线程安全，Stream 不共享）。0 = 按 CPU 核数自动。
# 工作线程只负责排队/调度，真正的并行度由 ONNX Runtime 的 intra-op 线程决定。
LIVE_ASR_WORKERS = int(os.environ.get("LIVENOTES_ASR_WORKERS", "0"))
LIVE_ASR_THREADS = int(os.environ.get("LIVENOTES_ASR_THREADS", "0"))
# 单会话最多允许多少条定稿排队；超出时丢最旧的定稿（计入 dropped，最终体现在
# 「未转写」时长里），保证慢会话不会吃光内存、也不会无限拖后。
LIVE_ASR_MAX_PENDING_FINALS = int(os.environ.get("LIVENOTES_ASR_MAX_PENDING_FINALS", "8"))
# 临时字幕排队超过这个时长就直接丢：下一帧会覆盖更长的音频，旧结果没有价值。
LIVE_PARTIAL_TTL_S = float(os.environ.get("LIVENOTES_PARTIAL_TTL_S", "2.5"))


def asr_workers() -> int:
    """ASR 调度线程数。默认给 10 路并发留足并行槽位，但不把线程开满。"""
    if LIVE_ASR_WORKERS > 0:
        return max(1, min(16, LIVE_ASR_WORKERS))
    n = os.cpu_count() or 2
    if n <= 4:
        return 2
    return max(2, min(8, n - 4))


def asr_threads() -> int:
    """单个解码请求使用的 ONNX Runtime intra-op 线程数（共享会话）。"""
    if LIVE_ASR_THREADS > 0:
        return max(1, min(8, LIVE_ASR_THREADS))
    n = os.cpu_count() or 2
    return max(1, min(4, n // 2))


# 直播中的「疑似认人」：只影响展示与飞书文案，不写底库、不标 verified。
# 每 20 秒对成形的簇比对一次；首次只给「疑似」，连续两次同一个人且满 30 秒
# 才去掉问号——飞书发出去收不回，宁可晚 20 秒。
LIVE_HINT_EVERY_S = float(os.environ.get("LIVENOTES_LIVE_HINT_EVERY", "20"))
LIVE_HINT_MIN_S = float(os.environ.get("LIVENOTES_LIVE_HINT_MIN", "20"))
LIVE_HINT_AUTO_S = float(os.environ.get("LIVENOTES_LIVE_HINT_AUTO", "30"))
LIVE_HINT_CONFIRM = int(os.environ.get("LIVENOTES_LIVE_HINT_CONFIRM", "2"))

# 采集与切分约定
SAMPLE_RATE = 16000          # 声纹/ASR 统一重采样到 16k 单声道
SEGMENT_SECONDS = 60         # 落盘分片长度，防止长直播崩溃丢全场
SUMMARY_BUCKET_SECONDS = 600 # 「按时间段」总结的默认分桶（10 分钟）

# 可选 LLM（不配置则使用本地抽取式总结，功能不降级为不可用）
LLM_BASE = os.environ.get("LIVENOTES_LLM_BASE", "").rstrip("/")
LLM_KEY = os.environ.get("LIVENOTES_LLM_KEY", "")
LLM_MODEL = os.environ.get("LIVENOTES_LLM_MODEL", "")
LLM_TIMEOUT = int(os.environ.get("LIVENOTES_LLM_TIMEOUT", "60"))
# 单次 socket 读写的超时（上面的 LLM_TIMEOUT）不封顶「整次请求」：中转站只要一直
# 慢慢吐字节，urlopen 就永远不会超时——实测有过一次收尾调用挂 7 分钟没回来。
# 这个值是整次调用的硬上限，到点就放弃、回退本地抽取式，绝不把收尾线程吊死。
LLM_TOTAL_TIMEOUT = int(os.environ.get("LIVENOTES_LLM_TOTAL_TIMEOUT", "75"))


def _env_int(name: str, default: int) -> int:
    """环境变量写错不该把服务带崩，回退默认值即可。"""
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


# ---- 录音归档与分享 ----
# 收尾后把 WAV 转成 m4a（AAC-LC 单声道）。32k 对语音足够，约为 WAV 的 1/8，
# 且 Chrome/Edge/Safari/飞书 WebView 都能直接播；opus 只在飞书语音片段上按需生成。
AUDIO_AAC_BITRATE = os.environ.get("LIVENOTES_AUDIO_AAC_BITRATE", "32k")
AUDIO_OPUS_BITRATE = os.environ.get("LIVENOTES_AUDIO_OPUS_BITRATE", "24k")
# 原 WAV 保留天数：0 = 转码成功后尽快删；-1 = 永久保留。默认 7 天，给抽查留窗口。
WAV_RETENTION_DAYS = _env_int("LIVENOTES_WAV_RETENTION_DAYS", 7)
# 归档音频保留天数：0 = 永久保留（默认）。要自动清理老录音时再设 30/90。
AUDIO_RETENTION_DAYS = _env_int("LIVENOTES_AUDIO_RETENTION_DAYS", 0)
# 飞书片段缓存与分享包输出目录（都在 data/ 下，不提交）
CLIPS_DIR = DATA_DIR / "clips"
SHARE_DIR = DATA_DIR / "share"
# 周报/复盘 Markdown：中间产物，默认保留 30 天（见 app/report.cleanup）。
REPORTS_DIR = DATA_DIR / "reports"


# ---- 全局 LLM 任务调度（app/scheduler.py）----
# 同时最多打几个模型请求。10 个直播间一起下播时，收尾任务会排队而不是一起打出去。
LLM_CONCURRENCY = max(1, min(16, _env_int("LIVENOTES_LLM_CONCURRENCY", 2)))
# 单次调用遇到 429/5xx/网络错误时的最大尝试次数（含第一次）。
LLM_MAX_ATTEMPTS = max(1, min(8, _env_int("LIVENOTES_LLM_MAX_ATTEMPTS", 3)))
# 指数退避：base * 2^(n-1)，加抖动，封顶 cap；设为 0 可关闭等待（测试用）。
LLM_BACKOFF_BASE_S = max(0.0, _env_float("LIVENOTES_LLM_BACKOFF_BASE", 1.0))
LLM_BACKOFF_MAX_S = max(0.0, _env_float("LIVENOTES_LLM_BACKOFF_MAX", 30.0))
# token 预算：0 = 不限制，也是默认值。总结/标注跑一半被预算掐断、退回抽取式的
# 体验太差（长直播尤其明显），所以默认不设上限；要限额时用环境变量显式打开。
LLM_SESSION_TOKEN_BUDGET = max(0, _env_int("LIVENOTES_LLM_SESSION_TOKEN_BUDGET", 0))
LLM_DAILY_TOKEN_BUDGET = max(0, _env_int("LIVENOTES_LLM_DAILY_TOKEN_BUDGET", 0))
# 送进总结的逐句上限：超过就按时间分层抽样（覆盖全场，而不是只取开头）。
LLM_SUMMARY_MAX_ROWS = max(50, _env_int("LIVENOTES_LLM_SUMMARY_MAX_ROWS", 800))
# 同一指纹的成功结果缓存多久（秒）。期间重复点击/收尾后立刻点总结都直接复用。
LLM_DEDUPE_TTL_S = max(0, _env_int("LIVENOTES_LLM_DEDUPE_TTL", 600))
# HTTP 请求等待一个排队任务完成的上限；超过就返回 504 + job_id，不假装成功。
LLM_TASK_TIMEOUT_S = max(1, _env_int("LIVENOTES_LLM_TASK_TIMEOUT", 600))
# 供应商不返回 usage 时，按这个上限估算输出 token，用于预算预占。
LLM_EXPECTED_OUTPUT_TOKENS = max(64, _env_int("LIVENOTES_LLM_EXPECTED_OUTPUT_TOKENS", 1200))
# 内存里保留最近多少条任务记录（同时落库，重启后仍能在任务面板看到）。
LLM_JOB_HISTORY = max(20, _env_int("LIVENOTES_LLM_JOB_HISTORY", 200))
# 可选的单价（每百万 token，人民币或美元都行，看账单口径）。0 = 不折算花费。
LLM_PRICE_INPUT_PER_MTOK = max(0.0, _env_float("LIVENOTES_LLM_PRICE_INPUT", 0.0))
LLM_PRICE_OUTPUT_PER_MTOK = max(0.0, _env_float("LIVENOTES_LLM_PRICE_OUTPUT", 0.0))

# ---- 飞书直播简报（app/livebrief.py）----
# 简报和结束时的正式总结共用调度器，但有自己的软上限：一次问答不能把整场
# 收尾的预算吃光。预算不足时一律回退本地抽取，不阻塞状态查询。
BRIEF_LLM = os.environ.get("LIVENOTES_BRIEF_LLM", "1").lower() not in ("0", "false", "no")
BRIEF_TOPICS_COOLDOWN_S = max(0.0, _env_float("LIVENOTES_BRIEF_TOPICS_COOLDOWN", 60))
BRIEF_SUMMARY_COOLDOWN_S = max(0.0, _env_float("LIVENOTES_BRIEF_SUMMARY_COOLDOWN", 120))
BRIEF_DAILY_CAP = max(0, _env_int("LIVENOTES_BRIEF_DAILY_CAP", 30))
BRIEF_SESSION_TOKEN_CAP = max(0, _env_int("LIVENOTES_BRIEF_SESSION_TOKEN_CAP", 30000))
BRIEF_SUMMARY_MAX_ROWS = max(30, _env_int("LIVENOTES_BRIEF_SUMMARY_MAX_ROWS", 350))
BRIEF_MAX_MINUTES = max(1, min(240, _env_int("LIVENOTES_BRIEF_MAX_MINUTES", 120)))
BRIEF_TOPICS_TTL_S = max(0.0, _env_float("LIVENOTES_BRIEF_TOPICS_TTL", 90))
BRIEF_SUMMARY_TTL_S = max(0.0, _env_float("LIVENOTES_BRIEF_SUMMARY_TTL", 300))

# ---- 可观测性（app/metrics.py）：采样间隔、保留天数、停滞判定 ----
# 快照落库间隔。15 秒足够还原「那几分钟」；再密就是给 SQLite 找活干。
METRICS_SAMPLE_S = max(5.0, _env_float("LIVENOTES_METRICS_SAMPLE_S", 15.0))
METRICS_RETENTION_DAYS = max(1, _env_int("LIVENOTES_METRICS_RETENTION_DAYS", 7))
# 内存里保留最近多少场的指标（已结束的场次也能在看板看到，不影响落库）。
SESSION_METRICS_MAX = max(8, _env_int("LIVENOTES_METRICS_SESSION_MAX", 64))
# 「有语音但多久没有新定稿」算一次停滞。60 秒足够排除一句长句的等待。
LIVE_STALL_S = max(10.0, _env_float("LIVENOTES_STALL_S", 60.0))
# 录内静默超过这个秒数就不算停滞：本来没人说话，不该报警。
LIVE_STALL_IDLE_S = max(5.0, _env_float("LIVENOTES_STALL_IDLE_S", 30.0))
LIVE_STALL_MIN_S = max(10.0, _env_float("LIVENOTES_STALL_MIN_S", 30.0))


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, CLIPS_DIR, SHARE_DIR, REPORTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
