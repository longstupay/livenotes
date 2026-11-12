# -*- coding: utf-8 -*-
"""SQLite 存储层：结构化发言记录 + 中文全文索引 + 说话人人工校正。

检索用 FTS5 trigram 与 LIKE 混合：
  - trigram 分词的 MATCH 至少要求 3 个字符，两字中文词（"跳槽"/"微信"）会静默返回 0 条；
    因此 <3 字符一律走 LIKE，>=3 字符优先 FTS，FTS 报错再回退 LIKE。
  - 这是实测过的坑（SQLite 3.53.1），不要"优化"掉。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from .config import DB_PATH, ensure_dirs

FTS_TRIGRAM = False  # init_db() 中按实际能力赋值

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                    TEXT PRIMARY KEY,
    title                 TEXT NOT NULL,
    started_at            TEXT NOT NULL,
    duration_s            REAL NOT NULL,
    source_mode           TEXT NOT NULL,      -- per_uid | mixed | demo
    sample_rate           INTEGER DEFAULT 16000,
    audio_path            TEXT,               -- 当前可播的整场音频（WAV 或 m4a）
    audio_format          TEXT,               -- wav | m4a | opus；空 = 老数据按后缀判断
    audio_original_path   TEXT,               -- 转码前的 WAV，按保留期删除
    audio_bytes           INTEGER,            -- 当前可播文件大小
    audio_transcoded_at   TEXT,               -- 收尾转码完成时间
    minutes_url           TEXT,               -- 飞书妙记分享链接（人工回填）
    overlap_seconds       REAL DEFAULT 0,     -- 被判定为重叠、未转写的时长
    untranscribed_seconds REAL DEFAULT 0,     -- 冷场/BGM/丢帧等无人声时长
    warnings_json         TEXT DEFAULT '[]',
    started_utc           TEXT,               -- 开始那一刻的 UTC（带偏移），换算北京时间用
    tz_offset_min         INTEGER,            -- 录制当时本机相对 UTC 的偏移（分钟，480 = 东八区）
    created_at            TEXT
);

CREATE TABLE IF NOT EXISTS utterances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    t_start         REAL NOT NULL,
    t_end           REAL NOT NULL,
    speaker_id      TEXT NOT NULL,            -- 聚类标签 SPK_00（分轨模式下为 uid）
    speaker_label   TEXT NOT NULL,            -- 人工确认后可改为昵称
    text            TEXT NOT NULL,
    asr_conf        REAL DEFAULT 1.0,
    overlap         INTEGER DEFAULT 0,       -- 1 = 多人重叠 / ASR 不可信
    category        TEXT DEFAULT 'substantive',
    value_tag       TEXT DEFAULT 'normal',
    importance      REAL DEFAULT 0.5,
    topic           TEXT DEFAULT '',
    low_value_reason TEXT DEFAULT '',
    edited_at       TEXT,                    -- 人工校对过（改字/改判）；重聚类不再覆盖
    created_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_u_stime    ON utterances(session_id, t_start);
CREATE INDEX IF NOT EXISTS idx_u_speaker  ON utterances(session_id, speaker_id);
CREATE INDEX IF NOT EXISTS idx_u_category ON utterances(session_id, category);
CREATE INDEX IF NOT EXISTS idx_u_value    ON utterances(session_id, value_tag);

CREATE TABLE IF NOT EXISTS speakers (
    session_id   TEXT NOT NULL,
    speaker_id   TEXT NOT NULL,
    display_name TEXT,
    verified     INTEGER DEFAULT 0,          -- 0 = 聚类标签，身份未确认
    note         TEXT DEFAULT '',
    PRIMARY KEY (session_id, speaker_id)
);

CREATE TABLE IF NOT EXISTS marks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    t            REAL NOT NULL,             -- 相对本场开头的秒数
    note         TEXT DEFAULT '',
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_m_session ON marks(session_id, t);

CREATE TABLE IF NOT EXISTS utterance_emb (
    utterance_id INTEGER PRIMARY KEY,        -- 一句一条声纹，供结束后全量重聚类
    session_id   TEXT NOT NULL,
    dim          INTEGER NOT NULL,
    vec          BLOB NOT NULL               -- 归一化后的 float16 小端，dim*2 字节
);
CREATE INDEX IF NOT EXISTS idx_e_session ON utterance_emb(session_id);

-- 跨场次的「人」（不是场次内的聚类标签）。persons 与 speakers 的区别：
--   speakers  = 这一场里第几号声音（SPK_00），出了这一场就作废
--   persons   = 真实世界的一个人，跨场次复用，靠 person_emb 里的声纹原型认出来
CREATE TABLE IF NOT EXISTS persons (
    person_id   TEXT PRIMARY KEY,           -- per_xxxxxxx，与场次无关
    name        TEXT NOT NULL,
    created_at  TEXT,
    note        TEXT DEFAULT ''
);

-- 一个人的声纹原型。同一人允许多条（不同场次/不同设备各存几条），
-- 比对时取与目标最像的那条，等价于「多数样本投票」的简化版。
CREATE TABLE IF NOT EXISTS person_emb (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   TEXT NOT NULL,
    session_id  TEXT DEFAULT '',            -- 这条原型来自哪一场（空 = 外部导入）
    speaker_id  TEXT DEFAULT '',
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL,              -- 归一化后的 float16 小端
    quality     REAL DEFAULT 1.0,           -- 该原型的可信度（时长为王）
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pe_person ON person_emb(person_id);

CREATE TABLE IF NOT EXISTS polished (
    session_id   TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,           -- 段落集合 + 模型 + 参数的指纹，变了就重算
    engine       TEXT NOT NULL,
    payload_json TEXT NOT NULL,           -- {"paragraphs":[{"i":0,"text":"..."}]}
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    scope        TEXT NOT NULL,              -- content | time | category | filtered
    filter_json  TEXT NOT NULL,
    engine       TEXT NOT NULL,
    fingerprint  TEXT DEFAULT '',            -- 输入指纹：文本+分类+主题+模型+筛选都没变就复用，0 token
    payload_json TEXT NOT NULL,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_s_scope ON summaries(session_id, scope);

-- AI 分析任务：一次点击 = 一条 run，阶段写在 stages_json 里，进度由后台线程更新。
-- 为什么落库而不是放内存：关掉页面、刷新、甚至服务重启后，用户回来还能看到
-- "这场跑到哪了 / 是不是已经跑完"，而不是面对一个看起来没反应过的按钮。
CREATE TABLE IF NOT EXISTS analysis_runs (
    id           TEXT PRIMARY KEY,           -- run_20261105_173712_ab12
    session_id   TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'full',   -- full | summary | label
    stages_json  TEXT NOT NULL DEFAULT '[]',     -- [{key,label,status,detail,ms}]
    status       TEXT NOT NULL,              -- queued | running | done | partial | failed | canceled | interrupted
    progress     INTEGER DEFAULT 0,          -- 0~100，给进度条用
    message      TEXT DEFAULT '',
    usage_json   TEXT DEFAULT '{}',
    error        TEXT DEFAULT '',
    started_at   TEXT, updated_at TEXT, finished_at TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_r_session ON analysis_runs(session_id, created_at DESC);

-- 显著性事件（W1：离线算出来的「本场最值得听的 N 段」）。
-- 一条 = 一段被合并过的窗口（相邻高光句 25s 内视为同一件事），不是单句。
-- level / pushed_at / feedback 是给 W2 的飞书推送预留的，现阶段不写。
CREATE TABLE IF NOT EXISTS highlights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    t_start      REAL NOT NULL,            -- 窗口起点（含前后留白，供回放试听）
    t_end        REAL NOT NULL,
    peak_t       REAL NOT NULL,            -- 峰值那一句的起点，深链用它
    kind         TEXT NOT NULL,            -- both | info | live
    level        TEXT NOT NULL,            -- P0 | P1
    score        REAL NOT NULL,            -- 排序用：max(info_z, arousal_z)
    info_z       REAL DEFAULT 0,
    arousal_z    REAL DEFAULT 0,
    why          TEXT DEFAULT '',          -- 「语速 1.9× 基线；两人抢麦」
    speaker_label TEXT DEFAULT '',
    text         TEXT DEFAULT '',          -- 峰值句原文
    n_utts       INTEGER DEFAULT 0,        -- 窗口里包含几句
    features_json TEXT DEFAULT '{}',       -- 峰值句的特征，便于复盘调权重
    feedback     INTEGER DEFAULT 0,        -- 1 = 有用 / -1 = 没用 / 0 = 没标
    feedback_at  TEXT,
    -- 人工标记的那一句；算法高光为 0。重算高光时靠它把人工标记原样保留。
    utterance_id INTEGER DEFAULT 0,
    created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_h_session ON highlights(session_id, score DESC);

-- 飞书推送台账：一条 = 一次「打算发出去的消息」。
-- 为什么不只 print 日志：用户问「为什么没收到」时，必须能回答是没命中、
-- 被预算拦了、还是发出去了对方没看；而且 (session_id,item) 唯一索引顺带
-- 做幂等——同一段高光不会因为重试/重启推两遍。
CREATE TABLE IF NOT EXISTS push_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    item         TEXT NOT NULL,            -- hl:<peak_t> 或 digest / daily
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed | skipped
    reason       TEXT DEFAULT '',          -- 失败/跳过原因：no_open_id / daily_cap / send_fail ...
    level        TEXT DEFAULT '',
    score        REAL DEFAULT 0,
    text         TEXT DEFAULT '',          -- 回退用的纯文本（也是人读的版本）
    card_json    TEXT DEFAULT '',          -- 实际发的卡片 JSON，改文案时能复盘
    features_json TEXT DEFAULT '{}',       -- 推送时的逐句特征，卡片反馈靠它归因
    feedback     INTEGER DEFAULT 0,       -- 卡片上点的 👍/👎
    feedback_at  TEXT,
    created_at   TEXT,
    sent_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_push_uniq ON push_log(session_id, item);
CREATE INDEX IF NOT EXISTS idx_push_time ON push_log(created_at);

-- 全局 LLM 调度的用量台账：按「天 + 场次 + 任务类型」累计，预算跨重启仍然有效。
-- day 用本地自然日；session_id 为空表示不属于任何场次的调用（目前只有探测类）。
CREATE TABLE IF NOT EXISTS llm_usage (
    day               TEXT NOT NULL,
    session_id        TEXT NOT NULL DEFAULT '',
    task              TEXT NOT NULL DEFAULT '',
    calls             INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT,
    PRIMARY KEY (day, session_id, task)
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_session ON llm_usage(session_id);

-- 任务台账：内存队列是执行真相，这张表负责「重启后还看得到谁失败了、能不能重试」。
CREATE TABLE IF NOT EXISTS llm_jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    session_id   TEXT NOT NULL DEFAULT '',
    fingerprint  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL,              -- queued | running | succeeded | failed
    priority     INTEGER NOT NULL DEFAULT 5,
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT DEFAULT '',
    created_at   TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    updated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_session ON llm_jobs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_fp ON llm_jobs(fingerprint, status);

-- 指标快照：每 LIVENOTES_METRICS_SAMPLE_S 秒一批。scope=global 一行，
-- scope=session 每个在跑的场次一行。事后复盘「那几分钟队列多深、解码有没有动」
-- 靠的就是它，而不是重启就清零的内存计数。
CREATE TABLE IF NOT EXISTS metrics_samples (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,              -- ISO 本地时间，字符串比较即时间序
    scope      TEXT NOT NULL,              -- global | session
    session_id TEXT NOT NULL DEFAULT '',
    data_json  TEXT NOT NULL DEFAULT '{}',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ms_ts ON metrics_samples(ts);
CREATE INDEX IF NOT EXISTS idx_ms_session ON metrics_samples(session_id, ts);

-- 关键事件流：warn/error 日志自动进这里，看板和 /api/metrics/events 直接读。
CREATE TABLE IF NOT EXISTS metric_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    level       TEXT NOT NULL DEFAULT 'info',
    event       TEXT NOT NULL,
    session_id  TEXT NOT NULL DEFAULT '',
    fields_json TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_me_ts ON metric_events(ts);
CREATE INDEX IF NOT EXISTS idx_me_event ON metric_events(event, ts);

-- 每场结束时的指标汇总：重启后仍能回答「这一场丢了多少、花了多少 token」。
CREATE TABLE IF NOT EXISTS session_metrics (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT DEFAULT '',
    finished_at TEXT DEFAULT '',
    data_json   TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sm_finished ON session_metrics(finished_at);
"""

_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS u_ai AFTER INSERT ON utterances BEGIN
  INSERT INTO utterances_fts(rowid, text, speaker_label, topic)
  VALUES (new.id, new.text, new.speaker_label, new.topic);
END;
CREATE TRIGGER IF NOT EXISTS u_ad AFTER DELETE ON utterances BEGIN
  INSERT INTO utterances_fts(utterances_fts, rowid, text, speaker_label, topic)
  VALUES ('delete', old.id, old.text, old.speaker_label, old.topic);
END;
CREATE TRIGGER IF NOT EXISTS u_au AFTER UPDATE ON utterances BEGIN
  INSERT INTO utterances_fts(utterances_fts, rowid, text, speaker_label, topic)
  VALUES ('delete', old.id, old.text, old.speaker_label, old.topic);
  INSERT INTO utterances_fts(rowid, text, speaker_label, topic)
  VALUES (new.id, new.text, new.speaker_label, new.topic);
END;
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """给老库补列，不改数据。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN deleted_at TEXT")
    # 录音归档：老库补列后 audio_format 为空，读取时按 audio_path 后缀兜底。
    for name, typ in (("audio_format", "TEXT"), ("audio_original_path", "TEXT"),
                      ("audio_bytes", "INTEGER"), ("audio_transcoded_at", "TEXT"),
                      ("minutes_url", "TEXT")):
        if name not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN %s %s" % (name, typ))
    ucols = {r["name"] for r in conn.execute("PRAGMA table_info(utterances)")}
    if "category_rule" not in ucols:      # AI 分类前把规则分类存一份，随时能还原
        conn.execute("ALTER TABLE utterances ADD COLUMN category_rule TEXT")
    if "value_tag_rule" not in ucols:
        conn.execute("ALTER TABLE utterances ADD COLUMN value_tag_rule TEXT")
    if "features_json" not in ucols:      # 显著性打分用的逐句特征（可空，老数据不影响）
        conn.execute("ALTER TABLE utterances ADD COLUMN features_json TEXT")
    # AI 标注指纹：= hash(提示词版本 + 模型 + 主题集 + 原文)。为空 = 这句从没进过模型；
    # 与当前指纹不符 = 主题集/模型换过，需要重标。增量分类就靠它，不再整场重打。
    if "ai_fp" not in ucols:
        conn.execute("ALTER TABLE utterances ADD COLUMN ai_fp TEXT")
    # 人工校对的句子不再被"重新分人"覆盖：用户改过的，机器就别再改回去。
    if "edited_at" not in ucols:
        conn.execute("ALTER TABLE utterances ADD COLUMN edited_at TEXT")
    if "ai_topics_json" not in cols:      # 上次 AI 标注用的主题集，重跑沿用同一套
        conn.execute("ALTER TABLE sessions ADD COLUMN ai_topics_json TEXT")
    # 时区：只有新场次写得进去。老数据保持 NULL，读取时按本机时区兜底，
    # 宁可"可能差一个小时"，也不要把一个猜出来的 UTC 永久写进库里。
    if "started_utc" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN started_utc TEXT")
    if "tz_offset_min" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN tz_offset_min INTEGER")
    # 卡片 JSON 是后加的：今天早些时候建的库已经有 push_log 了，CREATE TABLE IF NOT EXISTS
    # 不会补列，必须显式 ALTER，否则推送会一直报 no column。
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(push_log)")}
    if pcols and "card_json" not in pcols:
        conn.execute("ALTER TABLE push_log ADD COLUMN card_json TEXT DEFAULT ''")
    if pcols and "features_json" not in pcols:
        conn.execute("ALTER TABLE push_log ADD COLUMN features_json TEXT DEFAULT '{}'")
    if pcols and "feedback" not in pcols:
        conn.execute("ALTER TABLE push_log ADD COLUMN feedback INTEGER DEFAULT 0")
    if pcols and "feedback_at" not in pcols:
        conn.execute("ALTER TABLE push_log ADD COLUMN feedback_at TEXT")
    # 声纹身份：这一场的这一簇，被认为是谁（跨场次的 person，而不是场内的 SPK_xx）
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(speakers)")}
    if "person_id" not in scols:
        conn.execute("ALTER TABLE speakers ADD COLUMN person_id TEXT")
    if "person_score" not in scols:
        conn.execute("ALTER TABLE speakers ADD COLUMN person_score REAL")
    if "person_bg" not in scols:
        conn.execute("ALTER TABLE speakers ADD COLUMN person_bg REAL")
    if "person_state" not in scols:
        conn.execute("ALTER TABLE speakers ADD COLUMN person_state TEXT DEFAULT ''")
    # 标题归属：default | auto | auto_local | user | legacy —— 决定"能不能自动改"
    if "title_source" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN title_source TEXT")
    if "title_alts_json" not in cols:     # 最近一次总结起的备选名，改名框零请求直接读
        conn.execute("ALTER TABLE sessions ADD COLUMN title_alts_json TEXT")
    if "title_updated_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN title_updated_at TEXT")
    # 总结指纹：老库补列后全部为空，表示"历史结果无法判断是否过期"，下次照常重算
    sumcols = {r["name"] for r in conn.execute("PRAGMA table_info(summaries)")}
    if sumcols and "fingerprint" not in sumcols:
        conn.execute("ALTER TABLE summaries ADD COLUMN fingerprint TEXT DEFAULT ''")
    # 老库推断：默认名 = 系统起的；旧版「名字 · 03:41」也是系统起的（可以换掉）；
    # 其余一律当用户起的，永不自动覆盖。
    conn.execute(
        "UPDATE sessions SET title_source = CASE "
        "  WHEN title LIKE '抖音直播%' OR title LIKE '直播监听%' OR title LIKE '标签页直播%' "
        "  THEN 'default' "
        "  WHEN title GLOB '* · [0-9][0-9]:[0-9][0-9]' THEN 'auto' "
        "  ELSE 'legacy' END "
        "WHERE title_source IS NULL OR title_source = ''")
    # 已经按旧规则迁过一次的库：把「名字 · 03:41」这种系统名捞回 auto，
    # 条件是它从没被新 UI 改过（title_updated_at 为空），用户手改过的一律不动。
    conn.execute(
        "UPDATE sessions SET title_source = 'auto' "
        "WHERE title_source = 'legacy' AND title_updated_at IS NULL "
        "AND title GLOB '* · [0-9][0-9]:[0-9][0-9]'")
    # 高光事件的人工反馈：网页端点「有用/没用」直接写这里，W2 的推送预算也读它
    hcols = {r["name"] for r in conn.execute("PRAGMA table_info(highlights)")}
    if hcols and "feedback" not in hcols:
        conn.execute("ALTER TABLE highlights ADD COLUMN feedback INTEGER DEFAULT 0")
    if hcols and "feedback_at" not in hcols:
        conn.execute("ALTER TABLE highlights ADD COLUMN feedback_at TEXT")
    if hcols and "utterance_id" not in hcols:
        conn.execute(
            "ALTER TABLE highlights ADD COLUMN utterance_id INTEGER DEFAULT 0")
    # 上一次进程被杀 / 崩溃时留下的"还在跑"的任务：启动时一律收尾成 interrupted。
    # 后台线程已经不在了，留着 running 会让前端永远转圈，也让新任务以为"有一场在跑"。
    conn.execute(
        "UPDATE analysis_runs SET status='interrupted', error=COALESCE(NULLIF(error,''),"
        "'服务重启，这次分析中断了'), finished_at=COALESCE(finished_at,?), updated_at=? "
        "WHERE status IN ('queued','running')",
        (now(), now()))
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    global FTS_TRIGRAM
    conn.executescript(_SCHEMA)
    _migrate(conn)
    FTS_TRIGRAM = False
    if sqlite3.sqlite_version_info >= (3, 34, 0):
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS utterances_fts USING fts5("
                "text, speaker_label, topic, content='utterances', "
                "content_rowid='id', tokenize='trigram')"
            )
            conn.executescript(_FTS_TRIGGERS)
            # 外部内容表不会自动收录建表前就存在的行。老库升级上来、或上次进程在
            # 写入中途被杀，索引就会是空的/不一致；trigram 的 MATCH 这时静默返回 0 条，
            # 用户看到的是「升级后搜不到了」。启动时重建一次，单机小库的开销可以忽略。
            conn.execute("INSERT INTO utterances_fts(utterances_fts) VALUES('rebuild')")
            FTS_TRIGRAM = True
        except sqlite3.OperationalError:
            # 编译时未带 FTS5：功能降级为 LIKE，不阻断运行
            FTS_TRIGRAM = False
    conn.commit()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- 写入

def insert_session(conn: sqlite3.Connection, s: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sessions (id,title,started_at,duration_s,source_mode,"
        "sample_rate,audio_path,overlap_seconds,untranscribed_seconds,warnings_json,"
        "started_utc,tz_offset_min,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s["id"], s["title"], s["started_at"], s["duration_s"], s["source_mode"],
         s.get("sample_rate", 16000), s.get("audio_path"),
         s.get("overlap_seconds", 0), s.get("untranscribed_seconds", 0),
         json.dumps(s.get("warnings", []), ensure_ascii=False),
         s.get("started_utc"), s.get("tz_offset_min"), now()),
    )
    for sp in s.get("speakers", []):
        conn.execute(
            "INSERT OR REPLACE INTO speakers(session_id,speaker_id,display_name,verified,note) "
            "VALUES (?,?,?,?,?)",
            (s["id"], sp["speaker_id"], sp["display_name"], int(sp.get("verified", 0)),
             sp.get("note", "")),
        )
    conn.commit()


_U_COLS = ("session_id", "t_start", "t_end", "speaker_id", "speaker_label", "text",
           "asr_conf", "overlap", "category", "value_tag", "importance", "topic",
           "low_value_reason")
_U_DEFAULTS = {
    "session_id": "", "t_start": 0.0, "t_end": 0.0, "speaker_id": "SPK_??",
    "speaker_label": "未标识", "text": "", "asr_conf": 1.0, "overlap": 0,
    "category": "substantive", "value_tag": "normal", "importance": 0.5,
    "topic": "", "low_value_reason": "",
}


def insert_utterances(conn: sqlite3.Connection, rows: list[dict]) -> list[int]:
    """插入发言，返回自增 id 列表（实时链路要拿 id 挂声纹，见 save_embeddings）。"""
    sql = ("INSERT INTO utterances (" + ",".join(_U_COLS) + ",created_at) VALUES ("
           + ",".join("?" for _ in _U_COLS) + ",?)")
    ts = now()
    ids: list[int] = []
    for r in rows:
        cur = conn.execute(sql, tuple(r.get(c, _U_DEFAULTS[c]) for c in _U_COLS) + (ts,))
        ids.append(int(cur.lastrowid))
    conn.commit()
    return ids


# ---------------------------------------------------------------- 声纹

def save_embeddings(conn: sqlite3.Connection, sid: str,
                    items: list[tuple[int, bytes, int]]) -> None:
    """items = [(utterance_id, float16 bytes, dim)]。重复写入按覆盖处理。"""
    conn.executemany(
        "INSERT OR REPLACE INTO utterance_emb(utterance_id,session_id,dim,vec) VALUES (?,?,?,?)",
        [(i, sid, d, b) for i, b, d in items])
    conn.commit()


def load_embeddings(conn: sqlite3.Connection, sid: str) -> list[tuple[int, bytes, int]]:
    return [(int(r["utterance_id"]), r["vec"], int(r["dim"])) for r in conn.execute(
        "SELECT utterance_id,dim,vec FROM utterance_emb WHERE session_id=? "
        "ORDER BY utterance_id", (sid,))]


def count_embeddings(conn: sqlite3.Connection, sid: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM utterance_emb WHERE session_id=?",
                            (sid,)).fetchone()[0])


def apply_speakers(conn: sqlite3.Connection, sid: str,
                   rows: list[tuple[str, str, int]]) -> int:
    """整场重写说话人：rows = [(speaker_id, speaker_label, utterance_id)]。"""
    # edited_at 非空 = 人工改判过，重聚类不该把它改回去（用户会直接不信任这个按钮）
    cur = conn.executemany(
        "UPDATE utterances SET speaker_id=?, speaker_label=? "
        "WHERE id=? AND edited_at IS NULL", rows)
    conn.commit()
    return max(0, cur.rowcount)


def replace_speakers(conn: sqlite3.Connection, sid: str,
                     speakers: list[tuple[str, str, int]]) -> None:
    """重建 speakers 表：[(speaker_id, display_name, verified)]。"""
    conn.execute("DELETE FROM speakers WHERE session_id=?", (sid,))
    conn.executemany(
        "INSERT INTO speakers(session_id,speaker_id,display_name,verified,note) "
        "VALUES (?,?,?,?,'')", [(sid, i, n, v) for i, n, v in speakers])
    conn.commit()


def upsert_speaker(conn: sqlite3.Connection, sid: str, speaker_id: str,
                   display_name: str, note: str = "") -> None:
    """自动聚类标签；人工确认过（verified=1）的昵称不覆盖。"""
    conn.execute(
        "INSERT INTO speakers(session_id,speaker_id,display_name,verified,note) "
        "VALUES (?,?,?,0,?) ON CONFLICT(session_id,speaker_id) DO UPDATE SET "
        "display_name=excluded.display_name, note=excluded.note WHERE speakers.verified=0",
        (sid, speaker_id, display_name, note))
    conn.commit()


def relabel_speaker(conn: sqlite3.Connection, sid: str, speaker_id: str, label: str) -> int:
    """自动改标签（主播/嘉宾切换）。人工确认过（verified=1）的昵称保持不动。"""
    row = conn.execute("SELECT verified FROM speakers WHERE session_id=? AND speaker_id=?",
                       (sid, speaker_id)).fetchone()
    if row is not None and row["verified"]:
        return 0
    conn.execute("UPDATE utterances SET speaker_label=? WHERE session_id=? AND speaker_id=?",
                 (label, sid, speaker_id))
    conn.execute("UPDATE speakers SET display_name=? WHERE session_id=? AND speaker_id=? "
                 "AND verified=0", (label, sid, speaker_id))
    conn.commit()
    return conn.total_changes


def rename_session(conn: sqlite3.Connection, sid: str, title: str,
                   source: str = "user") -> int:
    """只改显示名，不动 id / 逐字稿 / 已保存的总结。

    source 默认 user：凡是走到改名这一步的，都当"人起的名字"，此后不再被自动总结覆盖。
    """
    conn.execute("UPDATE sessions SET title=?, title_source=?, title_updated_at=? WHERE id=?",
                 (title, source, now(), sid))
    conn.commit()
    return conn.total_changes


def set_title(conn: sqlite3.Connection, sid: str, title: str, source: str,
              alts: list[str] | None = None) -> int:
    """AI 命名写回：主标题 + 归属 + 同时备选（一次 UPDATE，避免两处状态不一致）。"""
    if alts is None:
        conn.execute("UPDATE sessions SET title=?, title_source=?, title_updated_at=? WHERE id=?",
                     (title, source, now(), sid))
    else:
        conn.execute(
            "UPDATE sessions SET title=?, title_source=?, title_alts_json=?, title_updated_at=? "
            "WHERE id=?", (title, source, json.dumps(alts, ensure_ascii=False), now(), sid))
    conn.commit()
    return conn.total_changes


def set_title_alts(conn: sqlite3.Connection, sid: str, alts: list[str]) -> int:
    """只更新备选名，不动正式标题、不动归属。"""
    conn.execute("UPDATE sessions SET title_alts_json=? WHERE id=?",
                 (json.dumps(list(alts or []), ensure_ascii=False), sid))
    conn.commit()
    return conn.total_changes


def touch_session(conn: sqlite3.Connection, sid: str, duration_s: float) -> None:
    conn.execute("UPDATE sessions SET duration_s=? WHERE id=?", (float(duration_s), sid))
    conn.commit()


def finish_session(conn: sqlite3.Connection, sid: str, duration_s: float,
                   audio_path: str | None, untranscribed_s: float = 0.0,
                   audio_format: str | None = None,
                   audio_bytes: int | None = None,
                   audio_original_path: str | None = None) -> None:
    conn.execute(
        "UPDATE sessions SET duration_s=?, audio_path=?, untranscribed_seconds=?, "
        "audio_format=COALESCE(?, audio_format), audio_bytes=COALESCE(?, audio_bytes), "
        "audio_original_path=COALESCE(?, audio_original_path) WHERE id=?",
        (float(duration_s), audio_path, float(untranscribed_s), audio_format,
         int(audio_bytes) if audio_bytes is not None else None,
         audio_original_path, sid))
    conn.commit()


_AUDIO_META_COLS = ("audio_path", "audio_format", "audio_original_path",
                    "audio_bytes", "audio_transcoded_at", "minutes_url")


def set_audio_meta(conn: sqlite3.Connection, sid: str, **fields: Any) -> int:
    """只更新录音元数据。允许显式传 None（表示清空），不碰其它列。"""
    keys = [k for k in fields if k in _AUDIO_META_COLS]
    if not keys:
        return 0
    sql = "UPDATE sessions SET " + ", ".join("%s=?" % k for k in keys) + " WHERE id=?"
    cur = conn.execute(sql, tuple(fields[k] for k in keys) + (sid,))
    conn.commit()
    return cur.rowcount


def set_minutes_url(conn: sqlite3.Connection, sid: str, url: str | None) -> int:
    return set_audio_meta(conn, sid, minutes_url=(url or None))


# ---------------------------------------------------------------- 文件中心

def trash_session(conn: sqlite3.Connection, sid: str) -> None:
    """软删除：只打时间戳，音频移到 data/trash 下，随时可还原。"""
    conn.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (now(), sid))
    conn.commit()


def restore_session(conn: sqlite3.Connection, sid: str) -> int:
    conn.execute("UPDATE sessions SET deleted_at=NULL WHERE id=?", (sid,))
    conn.commit()
    return conn.total_changes


def purge_session(conn: sqlite3.Connection, sid: str) -> None:
    """彻底删除：行 + 音频 + 声纹 + 润色。"""
    delete_session(conn, sid)


def drop_audio(conn: sqlite3.Connection, sid: str) -> int:
    """只删录音，保留逐字稿和总结——大多数时候这才是想要的。"""
    conn.execute(
        "UPDATE sessions SET audio_path=NULL, audio_format=NULL, "
        "audio_original_path=NULL, audio_bytes=NULL, audio_transcoded_at=NULL "
        "WHERE id=?", (sid,))
    conn.commit()
    return conn.total_changes


def list_trash(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM utterances u WHERE u.session_id=s.id) AS n_utterances "
        "FROM sessions s WHERE s.deleted_at IS NOT NULL ORDER BY s.deleted_at DESC")]


def delete_session(conn: sqlite3.Connection, sid: str) -> None:
    """取消一次没真正开始的采集，避免留下 0 秒的空场次。"""
    conn.execute("DELETE FROM utterances WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM speakers WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM summaries WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM marks WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM utterance_emb WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM highlights WHERE session_id=?", (sid,))
    conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
    conn.commit()


def insert_mark(conn: sqlite3.Connection, sid: str, t: float, note: str = "") -> dict:
    cur = conn.execute(
        "INSERT INTO marks(session_id,t,note,created_at) VALUES (?,?,?,?)",
        (sid, float(t), note, now()))
    conn.commit()
    return {"id": cur.lastrowid, "t": round(float(t), 1), "note": note}


def list_marks(conn: sqlite3.Connection, sid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id,t,note FROM marks WHERE session_id=? ORDER BY t", (sid,))]


# ---------------------------------------------------------------- 查询

def list_sessions(conn: sqlite3.Connection, include_trash: bool = False) -> list[dict]:
    where = "" if include_trash else "WHERE s.deleted_at IS NULL "
    return [dict(r) for r in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM utterances u WHERE u.session_id=s.id) AS n_utterances "
        "FROM sessions s " + where + "ORDER BY started_at DESC")]


def get_session(conn: sqlite3.Connection, sid: str) -> dict | None:
    return rowdict(conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())


def get_meta(conn: sqlite3.Connection, sid: str) -> dict:
    s = get_session(conn, sid)
    if not s:
        return {}
    speakers = [dict(r) for r in conn.execute(
        "SELECT sp.*, (SELECT COUNT(*) FROM utterances u WHERE u.session_id=sp.session_id "
        "  AND u.speaker_id=sp.speaker_id) AS n "
        "FROM speakers sp WHERE sp.session_id=? ORDER BY sp.speaker_id", (sid,))]
    cats = [dict(r) for r in conn.execute(
        "SELECT category, COUNT(*) AS n FROM utterances WHERE session_id=? "
        "GROUP BY category ORDER BY n DESC", (sid,))]
    s["warnings"] = json.loads(s.get("warnings_json") or "[]")
    return {"session": s, "speakers": speakers, "categories": cats,
            "marks": list_marks(conn, sid), "fts_trigram": FTS_TRIGRAM,
            "ai": ai_state(conn, sid)}


def _q_clause(q: str, use_fts: bool) -> tuple[str, list]:
    q = (q or "").strip().replace("\x00", "")
    if not q:
        return "", []
    like = (" AND (u.text LIKE ? OR u.speaker_label LIKE ? OR u.topic LIKE ?)",
            [f"%{q}%"] * 3)
    if len(q) < 3 or not use_fts or not FTS_TRIGRAM:
        return like
    # 用短语查询做子串匹配；双引号需转义
    phrase = '"' + q.replace('"', '""') + '"'
    return (" AND u.id IN (SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?)",
            [phrase])


def _where(f: dict, use_fts: bool = True) -> tuple[str, list]:
    w = ["u.session_id = ?"]
    p: list[Any] = [f["session_id"]]
    if f.get("t_from") is not None:
        w.append("u.t_end >= ?"); p.append(float(f["t_from"]))
    if f.get("t_to") is not None:
        w.append("u.t_start <= ?"); p.append(float(f["t_to"]))
    cats = f.get("categories") or []
    if cats:
        w.append("u.category IN (" + ",".join("?" * len(cats)) + ")"); p += list(cats)
    spks = f.get("speakers") or []
    if spks:
        w.append("u.speaker_id IN (" + ",".join("?" * len(spks)) + ")"); p += list(spks)
    if f.get("exclude_low_value"):
        w.append("u.value_tag != 'low'")
    if f.get("exclude_overlap"):
        w.append("u.overlap = 0")
    if f.get("min_conf") is not None:
        w.append("u.asr_conf >= ?"); p.append(float(f["min_conf"]))
    clause, qp = _q_clause(f.get("q", ""), use_fts)
    if clause:
        w.append(clause.lstrip(" AND "))
        p += qp
    return " AND ".join(x for x in w if x), p


def query_utterances(conn: sqlite3.Connection, f: dict, use_fts: bool = True,
                     limit: int = 2000, offset: int = 0) -> list[dict]:
    where, p = _where(f, use_fts)
    sql = ("SELECT u.* FROM utterances u WHERE " + where +
           " ORDER BY u.t_start ASC, u.id ASC LIMIT ? OFFSET ?")
    try:
        return [dict(r) for r in conn.execute(sql, p + [limit, offset])]
    except sqlite3.OperationalError:
        if use_fts:
            return query_utterances(conn, f, use_fts=False, limit=limit, offset=offset)
        raise


def count_utterances(conn: sqlite3.Connection, f: dict, use_fts: bool = True) -> int:
    where, p = _where(f, use_fts)
    try:
        return conn.execute("SELECT COUNT(*) FROM utterances u WHERE " + where, p).fetchone()[0]
    except sqlite3.OperationalError:
        if use_fts:
            return count_utterances(conn, f, use_fts=False)
        raise


def category_breakdown(conn: sqlite3.Connection, f: dict) -> dict[str, int]:
    where, p = _where(f, use_fts=False)
    rows = conn.execute("SELECT u.category, COUNT(*) n FROM utterances u WHERE " + where +
                        " GROUP BY u.category", p)
    return {r["category"]: r["n"] for r in rows}


def get_evidence(conn: sqlite3.Connection, sid: str, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    ids = [int(i) for i in ids][:200]
    ph = ",".join("?" * len(ids))
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM utterances WHERE session_id=? AND id IN ({ph}) ORDER BY t_start",
        [sid] + ids)]


def live_history(conn: sqlite3.Connection, sid: str, before_t: float | None = None,
                 limit: int = 120) -> list[dict]:
    """实时工作台往上翻：取 before_t 之前最近的 limit 条，按时间正序返回。"""
    where, params = "session_id=?", [sid]
    if before_t is not None:
        where += " AND t_start < ?"
        params.append(float(before_t))
    rows = conn.execute(
        "SELECT id,t_start,t_end,speaker_id,speaker_label,text FROM utterances "
        "WHERE " + where + " ORDER BY t_start DESC LIMIT ?", params + [int(limit)]).fetchall()
    return [dict(r) for r in reversed(rows)]


def speaker_display(label: str, person_name: str = "", state: str = "") -> str:
    """逐字稿/导出用的说话人显示：位置标签 + 认出来的人。

    数据库里 speaker_label 仍是位置标签（主播/嘉宾N），person 是后挂上去的；
    只在展示层拼，避免把"阿哲（疑似）"写死进 speaker_label 之后越拼越长。
    """
    base = (label or "").strip() or "说话人"
    name = (person_name or "").strip()
    cand = "（疑似）" if (state or "") == "cand" else ""
    if not name:
        return base + cand
    if name in base:                 # 人工改名后 label 本身就是人名，别再拼一遍
        return base if "疑似" in base else base + cand
    return "%s · %s%s" % (base, name, cand)


def session_transcript(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """逐字稿行带上人物身份，reader/export 才能显示"主播 · 阿哲（疑似）"。

    LEFT JOIN 是必须的：没认出人的簇没有 speakers.person_id，也要正常出行。
    """
    return [dict(r) for r in conn.execute(
        "SELECT u.t_start,u.t_end,u.speaker_id,u.speaker_label,u.text,u.category,"
        "u.value_tag,u.overlap,s.person_id,s.person_state,p.name AS person_name "
        "FROM utterances u "
        "LEFT JOIN speakers s ON s.session_id=u.session_id AND s.speaker_id=u.speaker_id "
        "LEFT JOIN persons p ON p.person_id=s.person_id "
        "WHERE u.session_id=? ORDER BY u.t_start", (sid,))]


def speaker_totals(conn: sqlite3.Connection, sid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT u.speaker_id, COALESCE(MAX(s.display_name), MAX(u.speaker_label)) "
        "AS speaker_label, MAX(s.person_id) AS person_id, "
        "MAX(s.person_state) AS person_state, MAX(p.name) AS person_name, "
        "MAX(s.verified) AS verified, "
        "COUNT(*) AS n, SUM(u.t_end - u.t_start) AS seconds "
        "FROM utterances u "
        "LEFT JOIN speakers s ON s.session_id=u.session_id AND s.speaker_id=u.speaker_id "
        "LEFT JOIN persons p ON p.person_id=s.person_id "
        "WHERE u.session_id=? GROUP BY u.speaker_id ORDER BY seconds DESC", (sid,))]


def topic_tally(conn: sqlite3.Connection, sid: str) -> list[tuple[str, int]]:
    """本场话题分布（不含"综合/未归类"），结构化标题用它补一档信息。"""
    return [(str(r["topic"]), int(r["n"])) for r in conn.execute(
        "SELECT topic, COUNT(*) AS n FROM utterances WHERE session_id=? AND topic != '' "
        "AND topic != '综合/未归类' GROUP BY topic ORDER BY n DESC", (sid,))]


def search_all(conn: sqlite3.Connection, q: str, limit: int = 60) -> list[dict]:
    """跨场次搜索。>=3 字走 FTS，短词回退 LIKE（trigram 对两字词无效）。"""
    q = (q or "").strip()[:40]
    if not q:
        return []
    if len(q) >= 3 and FTS_TRIGRAM:
        try:
            rows = conn.execute(
                "SELECT u.id,u.session_id,u.t_start,u.t_end,u.speaker_id,u.speaker_label,u.text,"
                "       u.category,s.title,s.started_at "
                "FROM utterances u JOIN sessions s ON s.id=u.session_id "
                "WHERE s.deleted_at IS NULL AND u.id IN "
                "  (SELECT rowid FROM utterances_fts WHERE utterances_fts MATCH ?) "
                "ORDER BY s.started_at DESC, u.t_start LIMIT ?",
                ('"' + q.replace('"', '""') + '"', limit)).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            pass
    rows = conn.execute(
        "SELECT u.id,u.session_id,u.t_start,u.t_end,u.speaker_id,u.speaker_label,u.text,"
        "       u.category,s.title,s.started_at "
        "FROM utterances u JOIN sessions s ON s.id=u.session_id "
        "WHERE s.deleted_at IS NULL AND u.text LIKE ? "
        "ORDER BY s.started_at DESC, u.t_start LIMIT ?",
        ("%" + q + "%", limit)).fetchall()
    return [dict(r) for r in rows]


def search_multi(conn: sqlite3.Connection, queries: list[str],
                 limit: int = 60) -> list[dict]:
    """多个检索式合并（术语拆分/别名扩展用），按场次时间倒序、句内时间正序。"""
    seen: set[int] = set()
    out: list[dict] = []
    for q in queries:
        for r in search_all(conn, q, limit):
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            out.append(r)
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # 场次新->旧；同一场内保持 search_all 的句内时间正序（sort 是稳定的）
    out.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return out


def timeline(conn: sqlite3.Connection, sid: str, bucket: int = 300,
             f: dict | None = None) -> list[dict]:
    """按时间桶统计各类发言条数，供前端时间轴密度条使用。"""
    where, p = _where(f or {"session_id": sid}, use_fts=False)
    rows = conn.execute(
        "SELECT CAST(u.t_start / ? AS INTEGER) AS b, u.category, "
        "       SUM(CASE WHEN u.overlap=1 THEN 1 ELSE 0 END) AS n_overlap, "
        "       COUNT(*) AS n "
        "FROM utterances u WHERE " + where + " GROUP BY b, u.category ORDER BY b",
        [bucket] + p)
    out: dict[int, dict] = {}
    for r in rows:
        d = out.setdefault(r["b"], {"bucket": r["b"], "t_from": r["b"] * bucket,
                                    "t_to": (r["b"] + 1) * bucket, "by_category": {},
                                    "total": 0, "overlap": 0})
        d["by_category"][r["category"]] = r["n"]
        d["total"] += r["n"]
        d["overlap"] += r["n_overlap"]
    return [out[k] for k in sorted(out)]


# ---------------------------------------------------------------- 说话人校正

def snapshot_rule_categories(conn: sqlite3.Connection, sid: str) -> None:
    """第一次跑 AI 分类时，把规则分类存进 *_rule 列（只存一次，不覆盖）。"""
    conn.execute(
        "UPDATE utterances SET category_rule=category, value_tag_rule=value_tag "
        "WHERE session_id=? AND category_rule IS NULL", (sid,))
    conn.commit()


def apply_ai_categories(conn: sqlite3.Connection, sid: str,
                        items: list[tuple[int, str, str, str, str]]) -> int:
    """写回 AI 标注。第五个字段是 ai_fp：下次只补 ai_fp 为空/不符的行。"""
    conn.executemany(
        "UPDATE utterances SET category=?, value_tag=?, topic=?, ai_fp=? "
        "WHERE id=? AND session_id=?",
        [(c, v, t, fp, i, sid) for i, c, v, t, fp in items])
    conn.commit()
    return len(items)


def restore_rule_categories(conn: sqlite3.Connection, sid: str) -> int:
    # 还原时必须把 ai_fp 清空：否则下次点「AI 分类」会因为"这句标过了"而全部跳过，
    # 表现成"按钮没反应"。清空后 = 一句都没标过，从头再来。
    cur = conn.execute(
        "UPDATE utterances SET category=category_rule, value_tag=value_tag_rule, ai_fp=NULL "
        "WHERE session_id=? AND category_rule IS NOT NULL", (sid,))
    conn.commit()
    return cur.rowcount


def has_ai_categories(conn: sqlite3.Connection, sid: str) -> bool:
    row = conn.execute("SELECT 1 FROM utterances WHERE session_id=? AND ai_fp IS NOT NULL "
                       "LIMIT 1", (sid,)).fetchone()
    return bool(row)


def ai_state(conn: sqlite3.Connection, sid: str) -> dict:
    """给前端一个"这批标签是规则还是模型给的"的说法（以前这个函数写了没人调）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN ai_fp IS NOT NULL THEN 1 ELSE 0 END) AS k "
        "FROM utterances WHERE session_id=?", (sid,)).fetchone()
    n = int(row["n"] or 0) if row else 0
    k = int(row["k"] or 0) if row else 0
    return {"labeled": k, "total": n, "engine": ("ai" if k else "rule"),
            "topics": _ai_topics(conn, sid)}


# ---------------------------------------------------------------- AI 分析任务

_RUN_FIELDS = ("status", "progress", "message", "usage_json", "error",
               "stages_json", "started_at", "finished_at")


def _run_row(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    for key in ("stages_json", "usage_json"):
        raw = d.pop(key, None) or ("[]" if key == "stages_json" else "{}")
        try:
            d[key[:-5]] = json.loads(raw)
        except (ValueError, TypeError):
            d[key[:-5]] = [] if key == "stages_json" else {}
    d["active"] = d.get("status") in ("queued", "running")
    return d


def create_run(conn: sqlite3.Connection, sid: str, kind: str, stages: list[dict]) -> dict:
    rid = "run_%s_%s" % (datetime.now().strftime("%Y%m%d_%H%M%S"), uuid.uuid4().hex[:4])
    t = now()
    conn.execute(
        "INSERT INTO analysis_runs(id,session_id,kind,stages_json,status,progress,message,"
        "usage_json,error,started_at,updated_at,finished_at,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, sid, kind, json.dumps(stages, ensure_ascii=False), "queued", 0,
         "排队中", "{}", "", None, t, None, t))
    conn.commit()
    return get_run(conn, rid)


def get_run(conn: sqlite3.Connection, rid: str) -> dict | None:
    return _run_row(conn.execute("SELECT * FROM analysis_runs WHERE id=?", (rid,)).fetchone())


def update_run(conn: sqlite3.Connection, rid: str, **fields) -> None:
    cols = [k for k in fields if k in _RUN_FIELDS]
    if not cols:
        return
    vals = []
    for c in cols:
        v = fields[c]
        if c in ("stages_json", "usage_json"):
            v = json.dumps(v, ensure_ascii=False)
        vals.append(v)
    cols.append("updated_at")
    vals.append(now())
    conn.execute("UPDATE analysis_runs SET " + ",".join("%s=?" % c for c in cols) + " WHERE id=?",
                 vals + [rid])
    conn.commit()


def get_latest_run(conn: sqlite3.Connection, sid: str) -> dict | None:
    # 按 rowid 而不是 created_at 排序：created_at 只到秒，同一秒里连跑两次会选错，
    # 前端就会拿着上一条 run 的进度画进度条（实测过，连点两次必现）。
    return _run_row(conn.execute(
        "SELECT * FROM analysis_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1",
        (sid,)).fetchone())


def get_active_run(conn: sqlite3.Connection, sid: str) -> dict | None:
    return _run_row(conn.execute(
        "SELECT * FROM analysis_runs WHERE session_id=? AND status IN ('queued','running') "
        "ORDER BY rowid DESC LIMIT 1", (sid,)).fetchone())


def list_runs(conn: sqlite3.Connection, sid: str, limit: int = 10) -> list[dict]:
    return [_run_row(r) for r in conn.execute(
        "SELECT * FROM analysis_runs WHERE session_id=? ORDER BY rowid DESC LIMIT ?",
        (sid, limit))]



def _ai_topics(conn: sqlite3.Connection, sid: str) -> list[str]:
    s = conn.execute("SELECT ai_topics_json FROM sessions WHERE id=?", (sid,)).fetchone()
    if not s:
        return []
    try:
        v = json.loads(s["ai_topics_json"] or "[]")
    except ValueError:
        return []
    return [str(x) for x in v if str(x).strip()]


def get_ai_topics(conn: sqlite3.Connection, sid: str) -> list[str]:
    return _ai_topics(conn, sid)


def set_ai_topics(conn: sqlite3.Connection, sid: str, topics: list[str]) -> None:
    conn.execute("UPDATE sessions SET ai_topics_json=? WHERE id=?",
                 (json.dumps(list(topics), ensure_ascii=False), sid))
    conn.commit()


def clear_ai_labels(conn: sqlite3.Connection, sid: str) -> int:
    """全部重标：把指纹抹掉，让每一句重新变脏（分类结果本身留着，直到被覆盖）。"""
    cur = conn.execute("UPDATE utterances SET ai_fp=NULL WHERE session_id=?", (sid,))
    conn.commit()
    return cur.rowcount


def set_utterance_speaker(conn: sqlite3.Connection, sid: str, utt_id: int,
                          speaker_id: str, speaker_label: str) -> int:
    """把单独一句改判给另一个说话人（只动这一条，不影响同人的其它句子）。"""
    cur = conn.execute(
        "UPDATE utterances SET speaker_id=?, speaker_label=?, edited_at=? "
        "WHERE id=? AND session_id=?",
        (speaker_id, speaker_label, now(), int(utt_id), sid))
    conn.commit()
    return cur.rowcount


def update_utterance(conn: sqlite3.Connection, sid: str, utt_id: int,
                     text: str | None = None, speaker_id: str | None = None,
                     speaker_label: str | None = None) -> int:
    """人工校对一句：改文字和/或改判说话人。

    改文字必须把 ai_fp / features_json 清空，否则下次 AI 分析会因为"指纹没变"
    跳过这句，用户改了字却看不到重新标注——这种"改了没反应"最伤口碑。
    """
    sets: list[str] = []
    params: list = []
    if text is not None:
        sets += ["text=?", "ai_fp=NULL", "features_json=NULL"]
        params.append(text)
    if speaker_id is not None:
        sets += ["speaker_id=?", "speaker_label=?"]
        params += [speaker_id, speaker_label or speaker_id]
    if not sets:
        return 0
    sets.append("edited_at=?")
    params.append(now())
    cur = conn.execute(
        "UPDATE utterances SET " + ",".join(sets) + " WHERE id=? AND session_id=?",
        params + [int(utt_id), sid])
    conn.commit()
    return cur.rowcount


def rename_speaker(conn: sqlite3.Connection, sid: str, speaker_id: str, name: str) -> int:
    conn.execute("UPDATE utterances SET speaker_label=? WHERE session_id=? AND speaker_id=?",
                 (name, sid, speaker_id))
    conn.execute("UPDATE speakers SET display_name=?, verified=1 WHERE session_id=? AND speaker_id=?",
                 (name, sid, speaker_id))
    conn.commit()
    return conn.total_changes


def merge_speakers(conn: sqlite3.Connection, sid: str, src_ids: list[str],
                   target_id: str, target_label: str) -> int:
    """把误拆的聚类标签合并到同一个人（混音兜底模式下最常见的校正动作）。"""
    if not src_ids:
        return 0
    ph = ",".join("?" * len(src_ids))
    conn.execute(
        f"UPDATE utterances SET speaker_id=?, speaker_label=? "
        f"WHERE session_id=? AND speaker_id IN ({ph})",
        [target_id, target_label, sid] + src_ids)
    conn.execute("DELETE FROM speakers WHERE session_id=? AND speaker_id IN (" + ph + ")",
                 [sid] + src_ids)
    conn.execute("UPDATE speakers SET display_name=?, verified=1 "
                 "WHERE session_id=? AND speaker_id=?", (target_label, sid, target_id))
    conn.commit()
    return len(src_ids)


# ---------------------------------------------------------------- 人物底库

def upsert_person(conn: sqlite3.Connection, person_id: str, name: str,
                  note: str = "") -> str:
    conn.execute(
        "INSERT INTO persons(person_id,name,created_at,note) VALUES (?,?,?,?) "
        "ON CONFLICT(person_id) DO UPDATE SET name=excluded.name",
        (person_id, name, now(), note))
    conn.commit()
    return person_id


def add_person_emb(conn: sqlite3.Connection, items: list[tuple]) -> int:
    """items = [(person_id, session_id, speaker_id, dim, bytes, quality)]。"""
    conn.executemany(
        "INSERT INTO person_emb(person_id,session_id,speaker_id,dim,vec,quality,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [(p, s, sp, d, b, float(q), now()) for p, s, sp, d, b, q in items])
    conn.commit()
    return len(items)


def person_protos(conn: sqlite3.Connection, person_id: str | None = None) -> list[tuple]:
    """[(person_id, dim, bytes, quality)]；不传 person_id 时返回全库（一次比对够用）。"""
    if person_id:
        rows = conn.execute(
            "SELECT person_id,dim,vec,quality FROM person_emb WHERE person_id=?", (person_id,))
    else:
        rows = conn.execute("SELECT person_id,dim,vec,quality FROM person_emb")
    return [(r["person_id"], int(r["dim"]), r["vec"], float(r["quality"] or 1.0))
            for r in rows]


def count_person_emb(conn: sqlite3.Connection, person_id: str) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM person_emb WHERE person_id=?",
                            (person_id,)).fetchone()[0])


def trim_person_emb(conn: sqlite3.Connection, person_id: str, keep: int) -> int:
    """只保留 quality 最高的 keep 条原型，防止一个人攒几百条把库拖脏。"""
    cur = conn.execute(
        "DELETE FROM person_emb WHERE person_id=? AND id NOT IN ("
        " SELECT id FROM person_emb WHERE person_id=? ORDER BY quality DESC, id DESC LIMIT ?)",
        (person_id, person_id, int(keep)))
    conn.commit()
    return cur.rowcount


def list_persons(conn: sqlite3.Connection) -> list[dict]:
    """人物列表 + 出现统计。只统计 auto/confirmed，被否决（rejected）的不算。"""
    rows = conn.execute(
        "SELECT p.person_id, p.name, p.created_at, p.note,"
        " (SELECT COUNT(*) FROM person_emb e WHERE e.person_id=p.person_id) AS n_protos,"
        " (SELECT COUNT(DISTINCT s.session_id) FROM speakers s"
        "   WHERE s.person_id=p.person_id AND s.person_state IN ('auto','confirmed')) AS n_sessions,"
        " (SELECT MAX(se.started_at) FROM speakers s JOIN sessions se ON se.id=s.session_id"
        "   WHERE s.person_id=p.person_id AND s.person_state IN ('auto','confirmed')) AS last_seen,"
        " (SELECT COALESCE(SUM(u.t_end-u.t_start),0) FROM speakers s"
        "   JOIN utterances u ON u.session_id=s.session_id AND u.speaker_id=s.speaker_id"
        "   WHERE s.person_id=p.person_id AND s.person_state IN ('auto','confirmed')) AS seconds"
        " FROM persons p ORDER BY n_sessions DESC, last_seen DESC, p.name").fetchall()
    return [dict(r) for r in rows]


def find_persons(conn: sqlite3.Connection, q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return list_persons(conn)
    like = "%" + q.replace("%", "") + "%"
    rows = conn.execute(
        "SELECT person_id,name,created_at,note FROM persons WHERE name LIKE ? "
        "ORDER BY name LIMIT 50", (like,)).fetchall()
    return [dict(r) for r in rows]


def get_person(conn: sqlite3.Connection, person_id: str) -> dict | None:
    r = conn.execute("SELECT person_id,name,created_at,note FROM persons WHERE person_id=?",
                     (person_id,)).fetchone()
    return dict(r) if r else None


def person_appearances(conn: sqlite3.Connection, person_id: str,
                       include_rejected: bool = False) -> list[dict]:
    """这个人在哪些场次、哪一段出现过。t_from/t_to 用于跳转音频。"""
    cond = "" if include_rejected else " AND s.person_state IN ('auto','confirmed')"
    rows = conn.execute(
        "SELECT s.session_id, s.speaker_id, s.display_name AS label,"
        " s.person_score, s.person_bg, s.person_state,"
        " (s.person_score + s.person_bg) AS person_raw,"
        " se.title, se.started_at, se.duration_s,"
        " (se.audio_path IS NOT NULL AND se.audio_path <> '') AS has_audio,"
        " MIN(u.t_start) AS t_from, MAX(u.t_end) AS t_to,"
        " COUNT(u.id) AS n, COALESCE(SUM(u.t_end-u.t_start),0) AS seconds"
        " FROM speakers s JOIN sessions se ON se.id=s.session_id"
        " LEFT JOIN utterances u ON u.session_id=s.session_id AND u.speaker_id=s.speaker_id"
        " WHERE s.person_id=?" + cond +
        " GROUP BY s.session_id, s.speaker_id ORDER BY se.started_at DESC", (person_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def set_speaker_person(conn: sqlite3.Connection, sid: str, speaker_id: str,
                       person_id: str | None, score: float | None = None,
                       bg: float | None = None, state: str = "auto") -> int:
    """把某场某簇认定为某人（person_id 为 None 时清空）。"""
    cur = conn.execute(
        "UPDATE speakers SET person_id=?, person_score=?, person_bg=?, person_state=? "
        "WHERE session_id=? AND speaker_id=?",
        (person_id, score, bg, state or "", sid, speaker_id))
    conn.commit()
    return cur.rowcount


def clear_person_claims(conn: sqlite3.Connection, sid: str, person_id: str,
                        keep_speaker_id: str) -> list[str]:
    """同场同人只留一个簇：把其它簇上的 person_id 清掉。

    为什么要在「确认/注册」时做：一人一场只能占一个座位，否则会出现
    043037 那种「SPK_01(auto) 和 SPK_02(confirmed) 都挂天外飞仙」；
    更糟的是确认会顺手把错簇的原型写进底库，越标越脏。
    这里只清身份，不删句子；被清掉的簇回到「说话人NN」，下次重聚类会再给位置标签。
    """
    rows = [dict(r) for r in conn.execute(
        "SELECT speaker_id FROM speakers WHERE session_id=? AND person_id=? AND speaker_id<>?",
        (sid, person_id, keep_speaker_id))]
    if not rows:
        return []
    for r in rows:
        spk = str(r["speaker_id"])
        digits = "".join(ch for ch in spk if ch.isdigit())
        label = ("说话人" + digits[-2:]) if digits else "说话人"
        conn.execute(
            "UPDATE speakers SET person_id=NULL, person_score=NULL, person_bg=NULL,"
            " person_state='', display_name=?, verified=0 "
            "WHERE session_id=? AND speaker_id=?", (label, sid, spk))
        conn.execute(
            "UPDATE utterances SET speaker_label=? WHERE session_id=? AND speaker_id=?",
            (label, sid, spk))
    conn.commit()
    return [str(r["speaker_id"]) for r in rows]


def clear_session_persons(conn: sqlite3.Connection, sid: str) -> None:
    """重跑检索前清掉「自动判定」。

    人工确认（confirmed）和人工否决（rejected）都保留：否决过的簇不要再被同一个人
    反复命中，否则用户点一次「不是他」等于白点。
    """
    conn.execute("UPDATE speakers SET person_id=NULL, person_score=NULL, person_bg=NULL,"
                 " person_state='' WHERE session_id=? AND (person_state IS NULL"
                 " OR person_state NOT IN ('confirmed','rejected'))", (sid,))
    conn.commit()


def delete_person(conn: sqlite3.Connection, person_id: str) -> None:
    """彻底移除一个人：原型和场次归属一起清掉（场次里的文字不动）。"""
    conn.execute("UPDATE speakers SET person_id=NULL, person_score=NULL, person_bg=NULL,"
                 " person_state='' WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM person_emb WHERE person_id=?", (person_id,))
    conn.execute("DELETE FROM persons WHERE person_id=?", (person_id,))
    conn.commit()


def merge_persons(conn: sqlite3.Connection, src_id: str, dst_id: str) -> int:
    """把认错的重复人物并到目标人物：原型、场次归属一起搬过去。"""
    if not src_id or not dst_id or src_id == dst_id:
        return 0
    conn.execute("UPDATE person_emb SET person_id=? WHERE person_id=?", (dst_id, src_id))
    conn.execute("UPDATE speakers SET person_id=? WHERE person_id=?", (dst_id, src_id))
    conn.execute("DELETE FROM persons WHERE person_id=?", (src_id,))
    conn.commit()
    return conn.total_changes


# ---------------------------------------------------------------- 总结缓存

def save_polished(conn: sqlite3.Connection, sid: str, fingerprint: str,
                  engine: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO polished(session_id,fingerprint,engine,payload_json,created_at) "
        "VALUES (?,?,?,?,?)",
        (sid, fingerprint, engine, json.dumps(payload, ensure_ascii=False), now()))
    conn.commit()


def get_polished(conn: sqlite3.Connection, sid: str) -> dict | None:
    row = conn.execute("SELECT * FROM polished WHERE session_id=?", (sid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except ValueError:
        d["payload"] = {}
    return d


def delete_polished(conn: sqlite3.Connection, sid: str) -> None:
    conn.execute("DELETE FROM polished WHERE session_id=?", (sid,))
    conn.commit()


def save_summary(conn: sqlite3.Connection, sid: str, scope: str, f: dict,
                 engine: str, payload: dict, fingerprint: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO summaries(session_id,scope,filter_json,engine,fingerprint,"
        "payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (sid, scope, json.dumps(f, ensure_ascii=False), engine, fingerprint or "",
         json.dumps(payload, ensure_ascii=False), now()))
    conn.commit()
    return cur.lastrowid


def summary_by_fingerprint(conn: sqlite3.Connection, sid: str, scope: str,
                           fingerprint: str) -> dict | None:
    """输入没变就复用上一次的总结：同一场连点两次，第二次 0 token。

    只认非空指纹——老结果指纹为空，一律当"不确定是否过期"，宁可重算。
    """
    if not fingerprint:
        return None
    row = conn.execute(
        "SELECT payload_json,created_at FROM summaries "
        "WHERE session_id=? AND scope=? AND fingerprint=? ORDER BY id DESC LIMIT 1",
        (sid, scope, fingerprint)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    payload["cached_at"] = row["created_at"]
    return payload


def latest_summary(conn: sqlite3.Connection, sid: str, scope: str) -> dict | None:
    """取这一场某个作用域最近一次生成的总结（含 payload），用于"切 tab 只读不重算"。"""
    row = conn.execute(
        "SELECT id,session_id,scope,engine,payload_json,created_at FROM summaries "
        "WHERE session_id=? AND scope=? ORDER BY id DESC LIMIT 1", (sid, scope)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except ValueError:
        d["payload"] = {}
    return d


def recent_summaries(conn: sqlite3.Connection, sid: str, scope: str,
                     limit: int = 20) -> list[dict]:
    """最近若干条总结（带 filter_json 与 payload），用来判断"这条是哪个口径的结果"。

    为什么不能只取最新一条：用户可能先跑整场、又跑一次带筛选的，后者写在后面。
    汇总段盲取最新，就会把"筛选后的要点"当成"整场的要点"重新盖章。
    """
    return [dict(r) for r in conn.execute(
        "SELECT id,scope,engine,filter_json,payload_json,created_at FROM summaries "
        "WHERE session_id=? AND scope=? ORDER BY id DESC LIMIT ?", (sid, scope, int(limit)))]


def list_summaries(conn: sqlite3.Connection, sid: str, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT id,session_id,scope,filter_json,engine,created_at FROM summaries "
        "WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, limit))]

# ---------------------------------------------------------------- 高光事件

_H_COLS = ("session_id", "t_start", "t_end", "peak_t", "kind", "level", "score",
           "info_z", "arousal_z", "why", "speaker_label", "text", "n_utts",
           "features_json", "feedback", "feedback_at", "utterance_id")
_H_DEFAULTS = {
    "session_id": "", "t_start": 0.0, "t_end": 0.0, "peak_t": 0.0,
    "kind": "info", "level": "P1", "score": 0.0, "info_z": 0.0, "arousal_z": 0.0,
    "why": "", "speaker_label": "", "text": "", "n_utts": 0, "features_json": "{}",
    "feedback": 0, "feedback_at": None, "utterance_id": 0,
}


def list_utterances_for_highlight(conn: sqlite3.Connection, sid: str) -> list[dict]:
    """按时间顺序取一场的发言（只取打分要用的列）。"""
    return [dict(r) for r in conn.execute(
        "SELECT id,t_start,t_end,speaker_id,speaker_label,text,asr_conf,overlap,"
        "category,value_tag,importance,topic,features_json FROM utterances "
        "WHERE session_id=? ORDER BY t_start, id", (sid,))]


def save_utterance_features(conn: sqlite3.Connection, pairs: list[tuple[str, int]]) -> None:
    """批量回写逐句特征。pairs = [(features_json, utterance_id), ...]"""
    if not pairs:
        return
    conn.executemany("UPDATE utterances SET features_json=? WHERE id=?", pairs)
    conn.commit()


def replace_highlights(conn: sqlite3.Connection, sid: str, rows: list[dict]) -> int:
    """整场重算：先清后插。人工打过的「有用/没用」按峰值时间点带过来，重算不该丢掉标注。

    人工标记（kind='manual'）不是算法产物，重算后原样放回去；否则用户
    刚在字幕行上标的高价值，下一次「重新分析」就消失了。
    """
    keep: dict[float, tuple[int, str | None]] = {
        round(float(r["peak_t"]), 1): (int(r["feedback"] or 0), r["feedback_at"])
        for r in conn.execute(
            "SELECT peak_t, feedback, feedback_at FROM highlights "
            "WHERE session_id=? AND feedback<>0", (sid,))}
    manual = [dict(r) for r in conn.execute(
        "SELECT * FROM highlights WHERE session_id=? AND kind='manual'", (sid,))]
    conn.execute("DELETE FROM highlights WHERE session_id=?", (sid,))
    sql = ("INSERT INTO highlights (" + ",".join(_H_COLS) + ",created_at) VALUES ("
           + ",".join("?" for _ in _H_COLS) + ",?)")
    ts = now()
    for r in rows:
        fb, fb_at = keep.get(round(float(r.get("peak_t") or 0.0), 1), (0, None))
        r["feedback"], r["feedback_at"] = fb, fb_at
        cur = conn.execute(sql, tuple(r.get(c, _H_DEFAULTS[c]) for c in _H_COLS) + (ts,))
        r["id"] = int(cur.lastrowid)      # 建完就能直接拿去打标 / 推送
    for r in manual:
        r.pop("id", None)
        conn.execute(sql, tuple(r.get(c, _H_DEFAULTS[c]) for c in _H_COLS)
                     + (r.get("created_at") or ts,))
    conn.commit()
    return len(rows) + len(manual)


def toggle_manual_highlight(conn: sqlite3.Connection, sid: str,
                            utterance_id: int = 0, t_start: float | None = None,
                            text: str = "", speaker_label: str = "") -> dict:
    """字幕行右键「标记高价值 / 取消标记」。

    优先按 utterance_id 找句；实时字幕还没拿到 id 时退到 t_start。
    返回 highlighted 表示操作后的状态，前端据此更新菜单和行样式。
    """
    row = None
    if utterance_id:
        row = conn.execute(
            "SELECT id,t_start,t_end,text,speaker_label FROM utterances "
            "WHERE id=? AND session_id=?", (int(utterance_id), sid)).fetchone()
    if row is None and t_start is not None:
        row = conn.execute(
            "SELECT id,t_start,t_end,text,speaker_label FROM utterances "
            "WHERE session_id=? AND ABS(t_start-?)<0.05 ORDER BY id LIMIT 1",
            (sid, float(t_start))).fetchone()
    if row is not None:
        uid = int(row["id"])
        t0 = float(row["t_start"] or 0.0)
        t1 = float(row["t_end"] or t0)
        body = str(row["text"] or text or "")
        who = str(row["speaker_label"] or speaker_label or "")
    else:
        uid = int(utterance_id or 0)
        t0 = float(t_start or 0.0)
        t1 = t0
        body = str(text or "")
        who = str(speaker_label or "")
    hit = conn.execute(
        "SELECT id FROM highlights WHERE session_id=? AND kind='manual' "
        "AND ((?<>0 AND utterance_id=?) OR (?=0 AND ABS(peak_t-?)<0.05)) "
        "ORDER BY id LIMIT 1",
        (sid, uid, uid, uid, t0)).fetchone()
    if hit:
        conn.execute("DELETE FROM highlights WHERE id=?", (int(hit["id"]),))
        conn.commit()
        return {"ok": True, "highlighted": False, "id": int(hit["id"])}
    cur = conn.execute(
        "INSERT INTO highlights (" + ",".join(_H_COLS) + ",created_at) VALUES ("
        + ",".join("?" for _ in _H_COLS) + ",?)",
        (sid, t0, t1, t0, "manual", "P0", 1000.0, 0.0, 0.0, "手动标记",
         who, body, 1, "{}", 0, None, uid, now()))
    conn.commit()
    return {"ok": True, "highlighted": True, "id": int(cur.lastrowid),
            "kind": "manual"}


def set_highlight_feedback(conn: sqlite3.Connection, sid: str, hid: int,
                           verdict: int) -> dict:
    """网页端点「有用/没用」。verdict: 1 / -1 / 0（0 = 撤销）。"""
    cur = conn.execute(
        "UPDATE highlights SET feedback=?, feedback_at=? WHERE id=? AND session_id=?",
        (int(verdict), now() if verdict else None, int(hid), sid))
    conn.commit()
    if cur.rowcount == 0:
        return {"updated": 0}
    return {"updated": 1, "id": int(hid), "feedback": int(verdict)}


def list_highlights(conn: sqlite3.Connection, sid: str, limit: int = 50) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM highlights WHERE session_id=? ORDER BY score DESC, t_start LIMIT ?",
        (sid, limit))]
    for r in rows:
        try:
            r["features"] = json.loads(r.pop("features_json") or "{}")
        except ValueError:
            r["features"] = {}
    return rows


def highlights_meta(conn: sqlite3.Connection, sid: str) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(created_at) AS built_at, "
        "SUM(CASE WHEN feedback<>0 THEN 1 ELSE 0 END) AS rated, "
        "SUM(CASE WHEN feedback>0 THEN 1 ELSE 0 END) AS good "
        "FROM highlights WHERE session_id=?", (sid,)).fetchone()
    return {"n": int(row["n"] or 0), "built_at": row["built_at"],
            "rated": int(row["rated"] or 0), "good": int(row["good"] or 0)}



# ---------------------------------------------------------------- 推送台账

# 一条"发送中"的占位超过这么久没被收尾，就认为上次进程在发送中途死了，
# 允许下一次推送接管它（否则这行既是"发送中"也是"永不发送"）。
PUSH_PENDING_STALE_S = 300


def claim_push(conn: sqlite3.Connection, session_id: str, item: str, *,
               level: str = "", score: float = 0.0, text: str = "",
               card_json: str = "", features_json: str = "") -> bool:
    """先占位再发送。返回 True = 这条归我发，False = 已经推过/正在推。

    唯一索引 (session_id,item) 是幂等的唯一保证：重试、双线程、进程重启后
    重放都不会把同一段高光发两遍。占位和发送分开，是为了让"发送中"也可见，
    而不是等飞书返回才知道。

    但"永不重复"不能变成"永远发不出去"：
      - failed / skipped 是终态，但都值得再试一次——凭证是后配的、网络是一次性的，
        占着坑不放会让这条高光在没有重试入口的情况下彻底消失；
      - pending 超过 5 分钟 = 上次进程在"占位之后、发送之前"死了，这行再没人收尾，
        不接管的话它既是"发送中"也是"永不发送"。
    接管时把台账刷新成这次的文案（重试带着新时间/新人名才有意义）。
    """
    t = now()
    cur = conn.execute(
        "INSERT OR IGNORE INTO push_log "
        "(session_id,item,status,level,score,text,card_json,features_json,created_at) "
        "VALUES (?,?,'pending',?,?,?,?,?,?)",
        (session_id, item, level, float(score or 0), text or "", card_json or "",
         features_json or "", t))
    claimed = cur.rowcount > 0
    if not claimed:
        # 时间字符串必须和 now() 同一种写法（ISO 的 T 分隔），否则 'T' > ' ' 会让
        # created_at < cutoff 永远不成立，僵尸占位就永远接管不了。
        stale = (datetime.now() - timedelta(seconds=PUSH_PENDING_STALE_S)).isoformat(
            timespec="seconds")
        cur = conn.execute(
            "UPDATE push_log SET status='pending', reason='', level=?, score=?, text=?,"
            " card_json=?, features_json=?, created_at=?, sent_at=NULL "
            "WHERE session_id=? AND item=? AND (status IN ('failed','skipped')"
            " OR (status='pending' AND created_at < ?))",
            (level, float(score or 0), text or "", card_json or "", features_json or "",
             t, session_id, item, stale))
        claimed = cur.rowcount > 0
    conn.commit()
    return claimed


def close_push(conn: sqlite3.Connection, session_id: str, item: str,
               status: str, reason: str = "") -> None:
    """收尾一条推送：sent / failed / skipped。失败也留痕，才能回答「为什么没收到」。"""
    conn.execute(
        "UPDATE push_log SET status=?, reason=?, sent_at=? WHERE session_id=? AND item=?",
        (status, reason or "", now() if status == "sent" else None, session_id, item))
    conn.commit()


def pushes_today(conn: sqlite3.Connection) -> int:
    """今天已经成功发出几条高光（不含本场回顾）。预算按"发成功"算：
    失败不该吃掉用户的额度——旧版是在发送前自增，一次网络抖动就白瞎一条。"""
    day = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM push_log WHERE status='sent' "
        "AND item LIKE 'hl:%' AND substr(created_at,1,10)=?", (day,)).fetchone()
    return int(row["n"] or 0)


def pushed_peaks(conn: sqlite3.Connection, sid: str) -> set[float]:
    """本场已经成功推出去的峰值时间点，用来在回顾里标「已推/未推」。"""
    out: set[float] = set()
    for r in conn.execute(
            "SELECT item FROM push_log WHERE session_id=? AND status='sent' "
            "AND item LIKE 'hl:%'", (sid,)):
        try:
            out.add(round(float(str(r["item"])[3:]), 2))
        except ValueError:
            continue
    return out


def push_history(conn: sqlite3.Connection, sid: str, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT item,status,reason,level,score,created_at,sent_at FROM push_log "
        "WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, int(limit)))]



def log_push_skip(conn: sqlite3.Connection, session_id: str, item: str, reason: str,
                  level: str = "", score: float = 0.0, text: str = "",
                  features_json: str = "") -> None:
    """记录"本来该推、但被预算/学习拦下"的决策。

    没有这一行，用户问"为什么这条没推"就只能翻代码。reason 用短码，
    人话在页面上映射（见 app/web/pushes.html）。
    """
    if not claim_push(conn, session_id, item, level=level, score=score,
                      text=text, features_json=features_json):
        return
    close_push(conn, session_id, item, "skipped", reason)


def set_push_feedback(conn: sqlite3.Connection, session_id: str, item: str,
                      verdict: int) -> int:
    """卡片上点 👍/👎：写回台账。item = hl:<peak>，与发送时同一把钥匙。"""
    cur = conn.execute(
        "UPDATE push_log SET feedback=?, feedback_at=? WHERE session_id=? AND item=?",
        (int(verdict), now() if verdict else None, session_id, item))
    conn.commit()
    return cur.rowcount


def set_highlight_feedback_at(conn: sqlite3.Connection, sid: str, peak: float,
                              verdict: int) -> int:
    """同一个峰值时间点如果也有高光行，一并写，网页和卡片状态保持一致。"""
    cur = conn.execute(
        "UPDATE highlights SET feedback=?, feedback_at=? "
        "WHERE session_id=? AND ABS(peak_t-?)<0.05",
        (int(verdict), now() if verdict else None, sid, float(peak)))
    conn.commit()
    return cur.rowcount


def push_overview(conn: sqlite3.Connection, sid: str, limit: int = 200) -> dict:
    """一场的推送决策：发送、失败、跳过各多少，以及每条的原因与反馈。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT item,status,reason,level,score,text,created_at,sent_at,feedback "
        "FROM push_log WHERE session_id=? ORDER BY id DESC LIMIT ?", (sid, int(limit)))]
    cnt = {"sent": 0, "failed": 0, "skipped": 0, "pending": 0}
    for r in rows:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    return {"rows": rows, "counts": cnt, "daily_used": pushes_today(conn)}

# ---------------------------------------------------------------- LLM 调度台账

def record_llm_usage(day: str, session_id: str, task: str, prompt_tokens: int,
                     completion_tokens: int, total_tokens: int) -> None:
    """累加一次调用的用量。调用方负责容错：写台账失败不该挡住业务。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO llm_usage(day,session_id,task,calls,prompt_tokens,"
            "completion_tokens,total_tokens,updated_at) VALUES (?,?,?,1,?,?,?,?) "
            "ON CONFLICT(day,session_id,task) DO UPDATE SET "
            "calls=calls+1, prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
            "completion_tokens=completion_tokens+excluded.completion_tokens, "
            "total_tokens=total_tokens+excluded.total_tokens, "
            "updated_at=excluded.updated_at",
            (day, session_id or "", task or "", int(prompt_tokens or 0),
             int(completion_tokens or 0), int(total_tokens or 0), now()))
        conn.commit()
    finally:
        conn.close()


def llm_usage_total(day: str | None = None, session_id: str | None = None) -> int:
    where: list[str] = []
    args: list[Any] = []
    if day is not None:
        where.append("day=?")
        args.append(day)
    if session_id is not None:
        where.append("session_id=?")
        args.append(session_id)
    sql = "SELECT COALESCE(SUM(total_tokens),0) FROM llm_usage"
    if where:
        sql += " WHERE " + " AND ".join(where)
    conn = connect()
    try:
        row = conn.execute(sql, args).fetchone()
    finally:
        conn.close()
    return int(row[0] if row else 0)


def llm_usage_breakdown(day: str | None = None, session_id: str | None = None,
                        limit: int = 50) -> list[dict]:
    where: list[str] = []
    args: list[Any] = []
    if day is not None:
        where.append("day=?")
        args.append(day)
    if session_id is not None:
        where.append("session_id=?")
        args.append(session_id)
    sql = ("SELECT day,session_id,task,calls,prompt_tokens,completion_tokens,"
           "total_tokens,updated_at FROM llm_usage")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY total_tokens DESC LIMIT ?"
    args.append(max(1, int(limit)))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


_JOB_FIELDS = ("kind", "session_id", "fingerprint", "payload_json", "status", "priority",
               "attempts", "error", "created_at", "started_at", "finished_at", "updated_at")


def create_llm_job(job: dict) -> None:
    """插入一条任务台账。id 已存在时忽略（同一进程内不会重复提交同一个 id）。"""
    cols = ["id"] + [k for k in _JOB_FIELDS if k in job]
    vals = [job["id"]] + [job[k] for k in _JOB_FIELDS if k in job]
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO llm_jobs(%s) VALUES (%s)"
                     % (",".join(cols), ",".join("?" * len(cols))), vals)
        conn.commit()
    finally:
        conn.close()


def update_llm_job(job_id: str, **fields: Any) -> None:
    cols = [k for k in fields if k in _JOB_FIELDS]
    if not cols:
        return
    conn = connect()
    try:
        conn.execute("UPDATE llm_jobs SET %s, updated_at=? WHERE id=?"
                     % ", ".join("%s=?" % k for k in cols),
                     [fields[k] for k in cols] + [now(), job_id])
        conn.commit()
    finally:
        conn.close()


def get_llm_job(job_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM llm_jobs WHERE id=?", (job_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except ValueError:
        d["payload"] = {}
    return d


def list_llm_jobs(limit: int = 50, session_id: str | None = None) -> list[dict]:
    sql = ("SELECT id,kind,session_id,fingerprint,status,priority,attempts,error,"
           "created_at,started_at,finished_at FROM llm_jobs")
    args: list[Any] = []
    if session_id:
        sql += " WHERE session_id=?"
        args.append(session_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def mark_interrupted_llm_jobs() -> int:
    """进程退出时还在 queued/running 的任务，下次启动标记成 failed，允许重试。"""
    conn = connect()
    try:
        cur = conn.execute(
            "UPDATE llm_jobs SET status='failed', "
            "error=CASE WHEN COALESCE(error,'')='' "
            "  THEN '进程退出时任务尚未完成（可重试）' ELSE error END, "
            "finished_at=COALESCE(finished_at,?), updated_at=? "
            "WHERE status IN ('queued','running')", (now(), now()))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# ---------------------------------------------------------------- 可观测性台账

def record_metric_samples(rows: list[dict]) -> int:
    """批量写指标快照。采样线程每批一个连接，避免每行开一次库。"""
    if not rows:
        return 0
    ts_now = now()
    payload = [(r["ts"], r["scope"], r.get("session_id") or "",
                json.dumps(r.get("data") or {}, ensure_ascii=False, default=str),
                ts_now) for r in rows]
    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO metrics_samples(ts,scope,session_id,data_json,created_at) "
            "VALUES (?,?,?,?,?)", payload)
        conn.commit()
        return len(payload)
    finally:
        conn.close()


def list_metric_samples(since: str | None = None, until: str | None = None,
                        scope: str | None = None, session_id: str | None = None,
                        limit: int = 1000) -> list[dict]:
    """按时间正序返回快照；data_json 已解析，前端不用二次 JSON.parse。"""
    sql = "SELECT ts,scope,session_id,data_json FROM metrics_samples WHERE 1=1"
    args: list[Any] = []
    if since:
        sql += " AND ts>=?"
        args.append(str(since))
    if until:
        sql += " AND ts<=?"
        args.append(str(until))
    if scope:
        sql += " AND scope=?"
        args.append(str(scope))
    if session_id is not None:
        sql += " AND session_id=?"
        args.append(str(session_id))
    sql += " ORDER BY id ASC LIMIT ?"
    args.append(max(1, min(20000, int(limit))))
    conn = connect()
    try:
        out = []
        for r in conn.execute(sql, args):
            try:
                data = json.loads(r["data_json"] or "{}")
            except ValueError:
                data = {}
            out.append({"ts": r["ts"], "scope": r["scope"],
                        "session_id": r["session_id"], "data": data})
        return out
    finally:
        conn.close()


def record_metric_event(level: str, event: str, session_id: str = "",
                        fields: dict | None = None, ts: str | None = None) -> None:
    """写一条关键事件；调用方负责容错，这里只做落库。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO metric_events(ts,level,event,session_id,fields_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (ts or now(), str(level or "info"), str(event), session_id or "",
             json.dumps(fields or {}, ensure_ascii=False, default=str), now()))
        conn.commit()
    finally:
        conn.close()


def list_metric_events(since: str | None = None, limit: int = 100,
                       level: str | None = None) -> list[dict]:
    sql = "SELECT ts,level,event,session_id,fields_json FROM metric_events"
    where: list[str] = []
    args: list[Any] = []
    if since:
        where.append("ts>=?")
        args.append(str(since))
    if level:
        where.append("level=?")
        args.append(str(level))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(1000, int(limit))))
    conn = connect()
    try:
        out = []
        for r in conn.execute(sql, args):
            try:
                fields = json.loads(r["fields_json"] or "{}")
            except ValueError:
                fields = {}
            out.append({"ts": r["ts"], "level": r["level"], "event": r["event"],
                        "session_id": r["session_id"], "fields": fields})
        return out
    finally:
        conn.close()


def save_session_metrics(session_id: str, data: dict, started_at: str = "",
                         finished_at: str = "") -> None:
    """场次结束汇总。同场重复写（重启/重试）按 session_id 覆盖。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO session_metrics(session_id,started_at,finished_at,data_json,"
            "created_at) VALUES (?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "started_at=excluded.started_at, finished_at=excluded.finished_at, "
            "data_json=excluded.data_json",
            (session_id, started_at or "", finished_at or "",
             json.dumps(data or {}, ensure_ascii=False, default=str), now()))
        conn.commit()
    finally:
        conn.close()


def get_session_metrics(session_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM session_metrics WHERE session_id=?",
                           (session_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["data"] = json.loads(d.pop("data_json") or "{}")
    except ValueError:
        d["data"] = {}
    return d


def list_session_metrics(limit: int = 50) -> list[dict]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT session_id,started_at,finished_at,data_json FROM session_metrics "
            "ORDER BY finished_at DESC, created_at DESC LIMIT ?",
            (max(1, min(500, int(limit))),)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            data = json.loads(r["data_json"] or "{}")
        except ValueError:
            data = {}
        out.append({"session_id": r["session_id"], "started_at": r["started_at"],
                    "finished_at": r["finished_at"], "data": data})
    return out


def prune_metrics(days: int = 7) -> dict[str, int]:
    """删除过期快照与事件。session_metrics 保留 volume 很小，不跟着删。"""
    cutoff = (datetime.now() - timedelta(days=max(1, int(days)))).isoformat(
        timespec="seconds")
    conn = connect()
    try:
        s = conn.execute("DELETE FROM metrics_samples WHERE ts<?", (cutoff,))
        e = conn.execute("DELETE FROM metric_events WHERE ts<?", (cutoff,))
        conn.commit()
        return {"samples": int(s.rowcount or 0), "events": int(e.rowcount or 0)}
    finally:
        conn.close()
