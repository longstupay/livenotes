# 可观测性：每场计数、结构化日志与本地看板

目标很具体：**后台几分钟没有产出时，事后能查清卡在哪一段，而不是只能翻终端。**
实现不引入 Prometheus / OpenTelemetry 依赖，仍然是纯标准库 + SQLite。

代码位置：

| 文件 | 职责 |
|---|---|
| `app/metrics.py` | 线程安全计数器 / 延迟窗口、每场指标、采样线程、Prometheus 文本 |
| `app/logs.py` | JSONL 结构化日志、按天轮转、warn/error 自动进事件表 |
| `app/store.py` | `metrics_samples` / `metric_events` / `session_metrics` 三张表 |
| `app/asr_pool.py` | 既有 ASR 调度统计，直接作为解码侧口径复用 |
| `app/web/metrics.html` | 本地看板 `/metrics`，2 秒轮询，无构建步骤 |

## 指标口径

### 每场 counters

| 指标 | 含义 | 来源 |
|---|---|---|
| `live.chunks_in` | 收到的 PCM 块数（浏览器 4 块/秒） | `LiveSession.feed` |
| `live.chunks_dropped` | 会话队列满时丢弃的块数 | `LiveSession.feed` |
| `live.finals` | 真正落库的定稿句数 | `LiveSession.commit_final` |
| `live.final_commit_ms` | 这句话最后一个采样到达 → 定稿落库的墙钟延迟 | 同上 |
| `live.errors` | 采集线程异常次数 | `LiveSession._worker` |
| `live.stalls` / `live.stalls_recovered` | 停滞次数 / 恢复次数 | 停滞看门狗 |
| `push.sent` / `push.failed` / `push.skipped` / `push.duplicate` | 推送终态 | `live._send` |
| `push.latency_ms` | 占位 → 发送完成的耗时 | `live._send` |
| `llm.calls` / `llm.tokens` / `llm.prompt_tokens` / `llm.completion_tokens` | 模型调用与 token | `Scheduler.settle` |
| `llm.job_ms` | 任务从开始执行到终态 | `Scheduler._finish_ok/_finish_failed` |
| `llm.retries` / `llm.jobs_failed` | 重试与失败 | `llm.chat` / 调度器 |

解码侧（RTF、定稿/临时字幕 P50/P95、丢句数、排队）沿用 `app/asr_pool.py`
已有的统计，`GET /api/live/status?id=...` 的 `asr` 字段与看板口径完全一致。

### 全局 counters

- `http.requests`、`http.status.<code>`、`http.errors`
- `http.duration_ms`，以及 `http.duration.<路径组>_ms`
- `live.chunks_in` / `live.bytes_in` / `live.chunks_dropped` / `live.chunks_paused`
- `live.sessions_started` / `live.sessions_finished`
- `push.*`、`llm.*`（带场次的写入会同时累加到全局）

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/metrics` | 进程、全局、每场、ASR、LLM 预算、今日 token/推送、最近事件 |
| `GET /api/metrics?since=<ISO或epoch>&scope=global&limit=600` | 附带时间序列（采样落库） |
| `GET /api/metrics/series?since=...&scope=session&session_id=...` | 单场时间序列 |
| `GET /api/metrics/sessions?limit=40` | 历史场次汇总（跨重启） |
| `GET /api/metrics/events?since=...&level=warn&limit=100` | 关键事件流 |
| `GET /api/metrics?format=prometheus` | Prometheus 文本格式，已拍平为 `livenotes_*` |
| `GET /metrics` | 本地看板 |
| `GET /api/health` | 在原 `ok/llm` 之外补了 `asr/uptime_s/sessions/stalls` |

`/api/metrics` 与 `/api/health` 都不加载模型，没装 sherpa-onnx 时照常可用。

## 停滞看门狗

判定条件（全部满足）：

1. 会话在运行且未暂停；
2. 录内时长 ≥ 30 秒；
3. 最近 30 秒内 VAD 判定有人在说话（排除「本来没人说话」）；
4. 距离上一次定稿落库 ≥ 60 秒。

首次触发记一条 `live.stall`（warn，进事件表 + 看板标红）；持续停滞每 5 分钟
补一条；新定稿落库时记 `live.stall_recovered`。这样既不把冷场误报成故障，
也能在「有声音但 ASR/写库卡住」时留下时间点。

## 结构化日志

- 路径：`data/logs/app-YYYYMMDD.jsonl`，单文件超过 20MB 轮转为 `.1`，保留 7 天。
- 一行一个 JSON：`ts / level / event / session_id / msg` + 任意字段。
- 终端同时输出人读版本；`data/finalize.log` 继续保留，老排障习惯不受影响。
- `warn` / `error` 会同时写 `metric_events`，所以看板和 `/api/metrics/events`
  在进程重启后仍能看到。

示例：

```json
{"ts":"2026-11-06T12:30:01.120+08:00","level":"warn","event":"live.stall","session_id":"LIVE_20261106_120000","idle_s":4.2,"last_final_s":63.1,"queued":2,"dropped":0,"n_utterances":87,"elapsed_s":420.5}
```

## 落库与保留

| 表 | 内容 | 保留 |
|---|---|---|
| `metrics_samples` | 每 15 秒一批：`scope=global` 一行，每个在跑场次一行 | 7 天 |
| `metric_events` | warn/error 事件 | 7 天 |
| `session_metrics` | 每场结束一条汇总（counters + ASR + 推送 + 时长 + 错误） | 不自动删 |

采样线程只由 `run.py` 启动；测试与工具直接读内存快照，不会起后台线程。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIVENOTES_METRICS_SAMPLE_S` | `15` | 快照落库间隔（最小 5 秒） |
| `LIVENOTES_METRICS_RETENTION_DAYS` | `7` | 快照/事件保留天数 |
| `LIVENOTES_METRICS_SESSION_MAX` | `64` | 内存中保留的最近场次指标数 |
| `LIVENOTES_STALL_S` | `60` | 多久没有新定稿算停滞 |
| `LIVENOTES_STALL_IDLE_S` | `30` | 录内静默超过多久就不算停滞 |
| `LIVENOTES_STALL_MIN_S` | `30` | 开场多久后才开始判定 |
| `LIVENOTES_LLM_PRICE_INPUT` | `0` | 每百万输入 token 单价；0 = 不折算 |
| `LIVENOTES_LLM_PRICE_OUTPUT` | `0` | 每百万输出 token 单价 |

## 一次典型排障

1. 打开 `/metrics`，看「在跑场次」是否标红停滞；
2. 看该场「收块 / 丢块 / 定稿 / 丢句 / RTF / 定稿 P95」判断是采集、队列还是解码；
3. 拉最近一小时时序，确认那几分钟队列深度与定稿数是不是同时持平；
4. 看事件流里 `live.stall` / `llm.job_failed` / `push.failed` 的时间点；
5. 需要细节时 grep 当天 JSONL：

```bash
grep LIVE_20261106_120000 data/logs/app-20261106.jsonl
```
