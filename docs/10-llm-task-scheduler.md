# LLM 全局任务调度：并发、退避、预算与幂等

所有业务模型调用（下播收尾、生成总结、润色、AI 分类）现在都走同一条队列。
以前每场 `stop()` 起一个线程直接打模型，10 个直播间同时下播就是 10 个请求一起出去；
现在只产生 10 条排队任务，真正并发由固定 worker 池控制，失败会退避重试，
同一份输入重复点击不会产生第二次调用（AI 分类的分片在跑时会合并；
跑完后允许重新分类，见下文）。

代码位置：

| 文件 | 职责 |
|---|---|
| `app/scheduler.py` | 队列、worker、指纹、重试退避、预算、任务台账 |
| `app/tasks.py` | 业务任务注册（`summary` / `polish` / `classify`），构造指纹输入 |
| `app/analysis_job.py` | 「AI 分析」整条 run 作为 `analysis_run` 任务排队，阶段进度仍写 `analysis_runs` |
| `app/llm.py` | 每次 `chat()` 前预占预算、取并发槽、失败退避、返回后结算 |
| `app/store.py` | `llm_jobs`（任务台账）与 `llm_usage`（用量台账）两张表 |

## 一次任务怎么走

```
HTTP / 下播回调 / AI 分析
   └─ tasks.submit_*()            生成 payload（筛选条件、内容摘要、提示词版本）
        └─ Scheduler.submit()     算指纹 → 命中缓存/在跑任务就直接复用
             └─ 入优先队列        priority 越小越先跑
                  └─ worker 取任务
                       └─ handler → analysis / polish / classify / analysis_job
                            └─ llm.chat()
                                 ├─ 预算预占（不够直接拒绝，不发请求）
                                 ├─ 取全局并发槽（最多 N 个请求同时出去）
                                 ├─ 失败：429/5xx/网络错误按指数退避重试
                                 └─ 成功：按 usage 结算，写 llm_usage
                  └─ 任务终态写 llm_jobs（succeeded / failed）
```

「AI 分析」整条 run 也占一个 worker 名额：一次点击不再起一条裸线程，
而是排队成 `analysis_run` 任务，run 里的每次模型调用仍各自去抢全局并发槽
（`LIVENOTES_LLM_CONCURRENCY`），所以多个 run 并行时总请求数依然有上限。
run 的阶段进度照旧写 `analysis_runs`，前端轮询 `/api/analysis/status` 不受影响；
排队期间状态是 `queued`，只有对应的调度任务已经结束且长时间没有进度才会标 `interrupted`。

HTTP 请求会等任务完成（最多 `LIVENOTES_LLM_TASK_TIMEOUT` 秒）。超时返回 **504 + `job_id`**，
任务仍在后台跑；前端可以拿 `job_id` 去 `/api/jobs` 查，而不是假装成功。

## 优先级

| 值 | 来源 | 说明 |
|---|---|---|
| 1 `PRIORITY_USER` | 用户点「生成总结」 | 用户在前端等着，最优先 |
| 2 `PRIORITY_ANALYSIS` | 润色、AI 分类、AI 分析 run | 交互式，但可以稍等 |
| 5 `PRIORITY_FINALIZE` | 下播自动收尾 | 后台任务，慢慢排队 |

同一指纹的任务已经在排队时，再次提交会**提升它的优先级**（用户点了按钮，就把它从收尾队列提到最前），
而不是再排一条。

## 并发上限

- worker 线程数 = `LIVENOTES_LLM_CONCURRENCY`（默认 **2**，范围 1~16）。
- 另有一把同样大小的信号量包住真正发 HTTP 的瞬间；直接调用 `llm.chat()` 的地方
  （例如自动标题）也要拿槽，所以总并发不会被绕过。
- 「测试连接 / 拉模型列表 / 逐个试」不排队、不占预算：它们本来就是排障用的，
  被预算或队列挡住反而没法修配置。它们只发最小请求（`ping`）。

## 指纹与幂等

指纹 = `SHA-256(算法版本 + 任务类型 + 场次 + 规范化输入 + 当前模型)`。

输入里只放**会改变模型输出**的东西：

| 任务 | 指纹输入 |
|---|---|
| `summary` | 筛选条件（时间/分类/说话人/关键词/低价值）、模式、引擎、内容摘要、提示词版本 |
| `polish` | 场次内容摘要、提示词版本、分段参数 |
| `classify` | 分片 offset/limit、话题列表、提示词版本（**不含**内容摘要） |
| `analysis_run` | 不参与去重（`cache_ttl=0`，同一场已有 active run 时由 `analysis_job.start` 直接返回） |

刻意**不放进指纹**的：

- `auto`：只影响失败后要不要用本地规则命名，不影响模型输入；
- `recluster`：只决定收尾前要不要按整场声纹重分人，不改变送给模型的内容；
- `force`：只表示「跳过成功缓存」，用户连点两下仍然应该复用正在跑的那次；
- `exclude_overlap` / `min_conf`：`build_scope` 里会被强制成固定值，勾不勾都不改变送给模型的内容；
- 分类任务的**内容摘要**：分类本身会改 `category`，摘要包含 `category`，放进去会导致
  「任务一开始跑，第二次点击就对不上正在跑的那一片」；分片位置 + 话题已足够定义同一片。

因此「下播自动总结」和「用户紧接着点生成总结」会命中同一个指纹，只调用一次。
收尾的 `recluster` 在 summary handler 内部先执行（按整场声纹重分人再总结），
不另起线程，所以服务退出时不会留下跑了一半的重聚类。

两种复用：

1. **正在跑**：直接返回同一个 `Job`，等它的结果；
2. **已成功**：`LIVENOTES_LLM_DEDUPE_TTL` 秒内（默认 600）直接返回缓存结果，`cached=true`。

`force=true` 只跳过第 2 种，仍会加入第 1 种。失败的任务**不进缓存**，可以重试。

AI 分类是分片推进的，`cache_ttl=0`：同一片正在跑时合并，跑完后允许重新分类
（比如先还原了规则）。它的指纹不含内容摘要，否则分类自己改 `category` 会让第二次点击对不上。

> 改了提示词要同步改对应的 `PROMPT_VERSION`（`analysis` / `polish` / `classify`），
> 否则指纹不变，旧结果会被当成新结果复用。

## 重试与退避

`llm.chat()` 内部按 `LIVENOTES_LLM_MAX_ATTEMPTS`（默认 3，含第一次）重试：

| 错误 | 是否重试 |
|---|---|
| 408 / 409 / 425 / 429 / 5xx | 是 |
| 网络错误、超时、连接被重置 | 是 |
| 400 / 401 / 403 / 404 | 否（key 错、模型不存在，重试只会再计费） |
| 200 但返回不是合法 JSON | 否 |
| 预算不足 `BudgetExceeded` | 否（调用方降级或返回 429） |

等待时间：`base * 2^(n-1)`，加 equal jitter（一半固定、一半随机），封顶
`LIVENOTES_LLM_BACKOFF_MAX`；服务端给了 `Retry-After` 就优先听它的。
默认 base=1s、cap=30s。

- 任务级的失败不会自动重跑整个任务——只有单次 HTTP 调用会重试。任务失败后
  `/api/jobs` 里状态为 `failed`，用户可以在任务面板点重试，或调 `/api/jobs/retry`。
- 总结在模型失败/预算不足时会降级成本地抽取式，任务状态仍是 `succeeded`，
  但结果**不会进去重缓存**（`llm_error` 字段有值）：修好 key 或额度后再点一次，
  会真正重新调用模型，而不是 10 分钟内都拿到同一份降级稿。

## token 预算

两层预算，**调用前预占、返回后按实际 usage 结算**。预占而不是只查已用，
才能挡住多个并发请求一起冲过上限。

| 预算 | 默认 | 环境变量 |
|---|---|---|
| 单场 | 不限制（`0`） | `LIVENOTES_LLM_SESSION_TOKEN_BUDGET` |
| 全局 / 自然日 | 不限制（`0`） | `LIVENOTES_LLM_DAILY_TOKEN_BUDGET` |

默认不设上限：总结/标注跑一半被预算掐断、整场退回抽取式的体验太差。
需要限额时用环境变量显式打开，`0` 表示不限制。用量按「日期 + 场次 + 任务」记在 `llm_usage` 表，重启不清零。
预算状态在 `/api/jobs` 的 `budgets` 字段里；`/api/health` 只返回队列/并发数。

预算耗尽时的行为：

- **总结**：不报错，回退到本地抽取式总结，`engine_reason` 会说明是预算不足还是模型调用失败；
- **润色 / AI 分类**：返回 **429**，错误信息带上已用/上限；
- **自动标题**：直接跳过，保留本地规则命名。

## 失败可重入与重启

- `llm_jobs` 存了 `kind + payload`，处理器按 `kind` 注册，所以进程重启后仍能重试。
- 进程启动时会把上次遗留的 `queued/running` 任务标成 `failed`（「服务中断，任务未完成（可重试）」），
  不会出现永远卡在「运行中」的幽灵任务。
- 主动 `shutdown` 时，队列里没跑的任务也会标成失败并注明可重试。
- 重试入口：`POST /api/jobs/retry {"job_id": "..."}`，或在 `/api/jobs` 里找到失败任务。

## 接口

| 接口 | 说明 |
|---|---|
| `GET /api/jobs?limit=50&session_id=...` | 最近任务 + 队列统计 + 预算状态 + 今日用量明细（`usage`） |
| `POST /api/jobs/retry` | 重试失败/取消的任务 |
| `GET /api/health` | `llm` 字段含 `queue` / `running` / `concurrency` |
| `POST /api/summarize/estimate` | 返回 `budget`，预算不足时 `would_use_llm=false` |
| `POST /api/summarize` / `/api/polish` / `/api/classify/ai` | 成功结果附带 `job` 元信息；超时 504 + `job_id` |

`/api/jobs` 里每条任务包含：`id`、`kind`、`session_id`、`status`（`queued/running/succeeded/failed/cancelled`）、
`attempts`、`error`、`priority`、`cached`、`ms`、`created_at`、`started_at`、`finished_at`。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIVENOTES_LLM_CONCURRENCY` | `2` | 同时打模型的请求上限（1~16） |
| `LIVENOTES_LLM_MAX_ATTEMPTS` | `3` | 单次调用最多尝试几次（含第一次，1~8） |
| `LIVENOTES_LLM_BACKOFF_BASE` | `1.0` | 退避基数（秒），0 表示不等待 |
| `LIVENOTES_LLM_BACKOFF_MAX` | `30` | 退避上限（秒） |
| `LIVENOTES_LLM_SESSION_TOKEN_BUDGET` | `0` | 单场 token 预算，0 = 不限 |
| `LIVENOTES_LLM_DAILY_TOKEN_BUDGET` | `0` | 全局每日 token 预算，0 = 不限 |
| `LIVENOTES_LLM_DEDUPE_TTL` | `600` | 成功结果复用窗口（秒） |
| `LIVENOTES_LLM_TASK_TIMEOUT` | `600` | HTTP 等任务完成的上限（秒），超时 504 |
| `LIVENOTES_LLM_EXPECTED_OUTPUT_TOKENS` | `1200` | 供应商不返回 usage 时用于预占的输出估算 |
| `LIVENOTES_LLM_JOB_HISTORY` | `200` | 内存里保留最近多少条任务（同时落库） |
| `LIVENOTES_LLM_SUMMARY_MAX_ROWS` | `800` | 送进总结的逐句上限，超过则按时间分层抽样 |

## 已知边界

- 单进程调度：任务队列和并发槽都在进程内，`tools/dev.py` 热重启前会等任务跑完。
  同一个 `data/` 目录不要同时起两个服务进程。
- 「测试连接 / 逐个试」按设计绕过预算和队列，只用于配置排障。
- 每日预算按**本机自然日**滚动，不是按 API 供应商的账单周期。
- 总结降级结果不缓存；但它仍会保存到 `summaries` 表，页面刷新看到的还是这份快照，
  再点「生成总结」才会重新调用。

## 自测

```bash
python -m unittest discover -s tests -v
```

覆盖：并发峰值不超限、优先级提升、在跑/已完成去重、`force` 语义、指纹稳定性与敏感性、
失败重试、等待超时、单场/每日/不限预算、可重试与非可重试错误、`Retry-After`、
usage 缺失兜底、下播与手动总结同指纹。
