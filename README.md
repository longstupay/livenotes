# LiveNotes / 直播语记

多人语音直播的结构化记录原型：把一路直播音频变成**带说话人的实时字幕、逐字稿、时间轴和总结**，并支持回放与导出。

纯本地运行，不依赖云服务；模型全部在本机推理，音频不出本机。

## 能做什么

- **实时监听**：在浏览器里选中正在播放的直播标签页，只采集这一路音频（不需要虚拟声卡）
- **三种采集底座**：默认「每个房间一个专属浏览器」，互不影响、最稳；设
  `LIVENOTES_CAPTURE_MODE=shared` 改成「一个 Edge 开多个房间标签页」，3 路实测
  浏览器内存 2.4GB（独立模式约 2.8GB）；设 `LIVENOTES_CAPTURE_MODE=direct`
  则从抖音 reflow 页取流、用 ffmpeg 直取，5 路并发实测 139MB；直取失败不会
  偷偷切浏览器，会在收听页停下来让你确认切换。
  见 [docs/02](docs/02-live-tab-capture.md#三种采集底座)
- **实时字幕**：主播开口约 1 秒后出现灰色「识别中」字幕，每 4 秒切句成定稿
- **说话人分离**：基于 CAM++ 声纹在线聚类，两位说话人轮流发言会自动分成「主播 / 嘉宾」
- **逐字稿与时间轴**：按说话人、关键词、时间筛选；点时间码直接跳到那一刻
- **录音分享**：收尾自动把 WAV 转 m4a（体积约为原来 1/10），飞书 `/分享 CH01 90秒`
  直接发原声片段，`/分享包` 导出摘要+字幕+音频给妙记，`/妙记` 回填链接后卡片带
  「整场回听」；`/周报`、`/复盘` 把多场总结、原话与妙记链接汇总成云文档（建不了
  云文档时自动发 Markdown）。见 [docs/16](docs/16-recording-sharing-options.md)
- **总结与画像**：整场总结、按时间段总结、主播画像；标题可让 LLM 生成，也可本地抽取
- **高光榜**：整场按「信息密度」「情绪高潮」两条轴打分，挑出最值得回听的 N 段，
  每条给出理由（语速 1.9× 基线 / 提到 3 处金额 / 两人抢麦）和 30 秒试听；
  页面上直接点 👍 / 👎 打标，统计行实时算「前 5 条命中几条」——验收不用命令行
- **关注词**：你说"关心什么"（如 FDE、OPC），命中的句子在打分时加权，更容易上榜和被推；
  网页高光榜上可直接增删
- **术语表**：登记"正确写法 + 常见误听"（O P C → OPC、欧匹西 → OPC），
  实时纠错、旧稿可补跑，检索也会按别名/中英混排拆词；见 `docs/09-edit-glossary.md`
- **逐句校对**：阅读页「✎ 校对」直接改字、改判说话人、合并/拆分说话人；
  改过的句子不会被"重新分人"覆盖
- **飞书机器人**：长连接收消息，监听中命中高光就主动推给你——卡片里时间是
  北京/UTC 两个字段、"回听"是按钮，卡片被拒自动回退纯文本；结束时补一条
  「没推到的高光」回顾。高光榜上的 👍/👎 会变成下一场的推送门槛（标过没用的少推）。
  卡片上的 👍/👎 一键回填偏好；`/pushes` 看每场"推了什么、没推为什么"。
  发「关注 FDE、OPC」「/话题 OPC政策」就能用，文案预览用 `tools/push_preview.py`，
  见 `docs/08-feishu-push-time.md`
- **贴链接就开听**：飞书里直接粘直播间链接（抖音/快手/B 站，分享出来的短链也行）
  就开始录——短链会先展开、换成标准直播间地址再存，同一个房间不会算两路。
  房间名显示成「主播名 · 房间标题」，不是 `webcast.amemv.com`，见
  [房间名从哪来](#房间名从哪来)。
  `/rooms` 看正在听哪几路、`/状态 [CH]` 看某一路有没有断流、`/stop 1` 停一路；
  陌生网站第一次弹卡片让你确认，点一次进长期白名单，以后不再问；个人主页 /
  短视频 / 网站首页这类链接直接挡回来并说明为什么。上限 5 路，常驻脚本是
  `tools/capture_agent.py`（负责拉起浏览器、崩了重拉）
- **直播简报**：飞书 `/状态 [CH]` 0 token 秒回现场；`/近聊 [CH] [30分钟]`
  给 3~5 个话题，每个带时间、原话和回听链接；`/总结 [CH]` 在后台生成开播至今
  的阶段性总结（长直播自动分层抽样）。多路时不猜：发选择卡，点某一路或回
  `1` / `CH01` 即可；选中即生成，文字 `/总结` 仍保留第一次确认。`/刷新` 重做
  上一条，`/控制台` 一张卡片看所有房间，`/订阅 10` 定时收近聊。模型没配、
  预算不足或证据不足时自动退回本地抽取并说明原因；见 `docs/14-feishu-live-brief.md`
- **导出**：纪要（Markdown）、字幕（SRT，带说话人前缀）
- **回放**：`/api/audio` 支持 `Range` 流式播放，拖动进度不整段重下
- **运行指标**：打开 `/metrics` 看每场「收块 / 丢块 / 定稿 / 丢句 / RTF / 定稿 P95 /
  推送 / 错误」，以及全局队列、token 花费和最近事件；日志是 JSONL，指标与事件落 SQLite，
  「有语音但 60 秒没有新定稿」会标红。见 `docs/12-observability.md`

## 收听页的状态从哪来

`/rooms` 上每一路的状态都不是前端猜的：

- **启动六步**：采集代理在拉起浏览器、打开页面、打开采集页时上报 `stage=1/2/3`，
  采集页在取音频、建会话、准备入库时继续上报 `stage=3/4/5`；第一块音频真的
  送到服务端后才变 `live`（`stage=6`）。失败时带 `failed_stage` 和原因，
  详情会停在真实的那一步。
- **运行健康**：`/api/live/status` 除了 `idle_s`（最后一句人声）还给
  `last_chunk_at`（最后收到音频块）和 `last_feed_at`（最后真正写进录音/入库）。
  「最近一块」看前者，安静但仍在推流时不会误报断流。
- **采集浏览器**：代理每轮 tick 往 `/api/capture/status` 发心跳（只存内存），
  详情页按心跳判断「运行中 / 已退出 / 心跳超时」，不再按房间状态推。
- **历史会话**：停止后房间保留 `session_id` 和 `saved` / `has_recording`；
  详情页用 `/api/live/utterances` + `/api/reader` 补最后几句和录音信息，
  服务重启后也能看。
- **直达某一路**：收听页的「实时字幕」跳到 `/live?session=<id>`，只展示这一路；
  这一路已经结束时，页面会给回放入口。
- **断流自动收尾**：采集页每 256ms 推一块音频（主播不说话也推），所以「超过
  `LIVENOTES_STALL_SECONDS`（默认 120 秒）一块都没到」只可能是链路断了——浏览器崩了、
  页面被关、网络断。服务端看门狗会把这一场按正常流程收掉：WAV 落盘、排队总结、
  发本场回顾，房间落到「已停止」并带上 `auto_stopped` / `auto_stop_reason`，
  飞书补一条「已自动收尾」。设 0 关闭。
  注意它看的是**音频块**不是人声：安静地坐着、流还在推，不算断流。
- **断流预警与自愈**：块停 30 秒（`LIVENOTES_STALL_WARN_SECONDS`）先发一条飞书预警，
  不结束场次，房间卡片标「采集断流」；块回来后自动把提示改成「采集已恢复」。
  采集页每 2 秒检查一次自己的产出，6 秒没块就上报异常并尝试恢复音频上下文。
  采集代理每 10 秒问一次服务端「最后一块多久之前」，超过 20 秒
  （`LIVENOTES_CAPTURE_RECOVER_SECONDS`）且直播间页面还在播，就关掉旧采集页、
  用同一个 session 重开，缺口留在同一场里；60 秒冷却避免反复重建。
  如果服务端已经按 120 秒收尾、但页面探针确认还在播，代理会把房间拉回 pending
  再重启采集，最多 3 次（`LIVENOTES_CAPTURE_RESTART_MAX`）——这一条专治
  「后半夜断流收尾后，直播其实还在播，剩下几小时全丢」。
- **扩展侧守护**：Chrome 内存紧时会回收 offscreen 文档，而且没有回调。扩展用
  1 分钟一次的 alarm ping 它，死了就重建，并按记住的标签页和 session 把采集续上。
- **共享模式**：一个 Edge 进程 + 每房间一个标签页，扩展往标签页里注入采集脚本，
  标签页静音不影响采集；页面被抖音暂停时，音频回调每 4 块把它拉回播放。
  浏览器整体崩了会带走所有共享房间，代理会把它们重新拉起来。
- **下播自动收尾**：采集代理每 60 秒（`LIVENOTES_RECHECK_SECONDS`）对每一路「在听」的
  直播间复查一次页面——主画面变成有头有尾的视频（推荐流/点播）、页面上写着
  「直播已结束」这类文案且主画面停了、或抖音页面已经离开 `live.douyin.com/<房号>`，
  都算这一场结束。连续 2 次（`LIVENOTES_END_CONFIRM`）才动手：停会话、落 WAV、
  发本场回顾，房间同样带 `auto_stopped` / `auto_stop_reason`。
  这一条治的是"下播后页面自动播推荐流、标签页音频轨根本不断"——那种情况下采集页
  会一直录下去，光靠断流看门狗是发现不了的。

## 房间名从哪来

收听页每一路显示的都不是域名，而是「主播名 · 房间标题」，比如
`示例主播 · 示例直播标题`。名字按可靠性和到达顺序分三层：

- **分享文案先垫**（`name_source=share`）：飞书里粘进来的分享文案本身就写着
  【主播名】正在直播，建房间时就先落这个名字。为的是"刚加进来那 5~10 秒"——
  这段时间采集端还没落地，只有域名的话根本认不出是哪一路。
  `【抖音】`这种平台名会被排除，见 `app/watchlink.py` 的 `share_label()`。
- **页面读到的真名**（`name_source=page`）：直取解析流地址时，reflow 页的
  首屏 JSON 里就有 `room.title` 和 `owner.nickname`，同一次抓取顺手解析，
  不额外请求、不开浏览器（`tools/direct_capture.py` 的 `parse_room_meta()`）。
  浏览器采集模式本来就靠标签页标题取名，`clean_title()` 会洗掉
  「 - 抖音直播」这类后缀。
- **人工改名**（`name_source=user`）：收听页详情里的「改名」。一旦改过，
  采集端后来读到的名字**不许覆盖**——否则改了又变回去，比没名字更糟。

名字跟着房间落盘（`data/watch_rooms.json`），重启、停止、换引擎都还在，
所以「停了之后要删哪一个」看名字和房号就能判断。抓不到名字时不会报错，
退回原来的域名显示——名字永远是锦上添花，不能挡住录音。

## 快速开始

### 一次性准备

```bash
pip install -r requirements-live.txt   # ASR/声纹依赖 + 按进程静音用的 pycaw
python tools/fetch_models.py           # 约 270MB 本地模型
```

飞书要用的话，在 `data/feishu.json` 里填 `app_id` / `app_secret`（该文件已在 .gitignore 里）。
桌面版的数据目录是 `%LOCALAPPDATA%\LiveNotes\data`，凭证放那里的 `feishu.json`；
开发态第一次启动时如果缺这个文件，引擎会从仓库 `data/feishu.json` 复制一份。
主人（唯一能发命令的人）用 `FEISHU_OWNER_OPEN_ID` 或 `data/feishu.json` 的
`owner_open_id` 固定；两个都没配时，第一个给机器人发消息的人成为主人，之后别人
发消息不会改写推送对象，也不会收到回复。

群聊默认关闭：把群的 `chat_id` 填进 `FEISHU_CHAT_IDS`（逗号分隔）或
`data/feishu.json` 的 `chat_ids`，机器人只响应白名单群里 **@机器人** 的消息。
想试自然语言再设 `FEISHU_NL_ENABLED=1`，仍然只认白名单群和 `@`，总结照旧先确认。

### 日常启动：三个进程

| # | 进程 | 命令 | 干什么 |
|---|---|---|---|
| 1 | 服务 | `python run.py --lan` | 网页、接口、录音入库。**必须第一个起** |
| 2 | 采集代理 | `python tools\capture_agent.py --base http://127.0.0.1:8765` | 贴一个链接就拉起一个专属 Chrome 去录，崩了重拉。共享模式：`set LIVENOTES_CAPTURE_MODE=shared`；抖音直取：`set LIVENOTES_CAPTURE_MODE=direct`，用同一条命令，需 ffmpeg 在 PATH（streamlink 可选兜底） |
| 3 | 飞书机器人 | `python tools\feishu_bot.py` | 长连接收消息（贴链接/确认/命令），推高光 |

抖音直取只需 `ffmpeg` 在 PATH；`streamlink` 仅在 reflow 页拿不到流时兜底，可选安装
（`pip install -r requirements-direct.txt`）。然后双击 `tools\start_direct.bat`
（等同于先设 `LIVENOTES_CAPTURE_MODE=direct` 再运行 `start.bat`）。
直取失败时默认停在收听页等你确认切换，不会自动打开浏览器；这个策略在「⚙ 设置 →
采集」里可改（`ask` / `auto` / `none`），改完立即生效，不用重启采集代理。
没装 ffmpeg 时继续用默认浏览器模式即可。

一键：**双击 `tools\start.bat`**，三个窗口各起一个进程，然后自动打开收听页。
停止：**双击 `tools\stop.bat`**（或直接关那三个窗口）。

> 桌面版（`desktop/`）不用手动起这三个：壳启动引擎时会设 `LIVENOTES_HELPERS=1`，
> 引擎拉起飞书机器人；房间按设置页「桌面版 → 采集方式」处理——
> 默认用应用内浏览器，也可以选抖音直取（ffmpeg）或外部 Chrome。
> 网页版不设这个变量，仍按上表手动或 `start.bat` 启动，两套互不影响；
> 同一个飞书机器人不要同时开两套，长连接会把消息分给其中一条。

起来之后打开 <http://127.0.0.1:8765/rooms>（收听页）。服务窗口里看到这两行就对了：

```
本机打开 : http://127.0.0.1:8765/
[feishu] 正在建立长连接…   connected to wss://msg-frontier.feishu.cn/...
```

> 网页里的「实时监听」（手动抓当前标签页）必须用 `127.0.0.1` 打开，浏览器才允许采集；
> 手机访问局域网 IP 只能看回放。

> 模型与运行数据不在仓库里：`models/` 由 `tools/fetch_models.py` 下载，`data/` 里的数据库和录音是本地运行产物。

## 测试与 CI

```bash
pip install -r requirements-dev.txt   # pytest + numpy + lark-oapi，故意不装 sherpa-onnx
python -m pytest                      # 全量回归
node extensions/livehub/selftest.mjs  # 插件 offscreen 自检
python tools/ui_smoke.py              # 前端冒烟：11 个页面逐个用 headless Chrome 打开
```

`tests/` 覆盖存储与 FTS 检索、显著性打分（滑动基线/冷启动/推送门槛）、术语纠错顺序、
设置迁移与偏好回流、老库 schema 迁移。`tests/test_live_pipeline.py` 用合成 PCM 把
「推流 → VAD 断句 → ASR/声纹 → 术语纠错 → 聚类 → SQLite/FTS → WAV」整条链路跑一遍，
模型边界用确定性假实现替换，不需要模型文件。`tests/test_degradation.py` 验证没装
sherpa-onnx 时服务照常启动、`/api/live/start` 返回 503 和安装提示，而不是崩溃。
`tests/test_metrics.py` 覆盖指标窗口、采样落库、事件流、结构化日志、停滞判定与
`/api/metrics` 的 HTTP 出口。

`tools/ui_smoke.py` 是前端改动的刹车：起一个临时服务（临时库 + 演示场），用 headless
Chrome 逐页打开，断言关键节点在 DOM 里、控制台没有 `Uncaught` / 语法错。拆模块之后
「某个 import 少了个名字 → 整页白屏」是最典型的故障，这条能在 15 秒内抓到；没有
Chrome 的机器上对应测试自动 skip，静态资源与 JS 语法检查仍然生效。

GitHub Actions 在 Python 3.11 与 3.13 上跑同一套测试，并单独跑插件自检；CI 只安装
测试依赖，降级路径本身就是验收项。

## 目录结构

```
run.py              一键启动：初始化库 -> 生成演示场 -> 起服务 -> 开浏览器
app/
  server.py         HTTP 服务与 API 路由（标准库，无 Web 框架）
  live.py           实时链路：AudioWorklet -> VAD 断句 -> ASR -> 声纹聚类
  asr_pool.py       共享 ASR 工作池：跨会话公平调度、定稿优先、临时字幕合并
  analysis.py       切句、统计、关键词
  coach.py          总结与主播画像
  highlight.py      显著性打分：逐句特征 -> 信息/情绪双轴 z 分数 -> 高光事件
  llm.py            可选 LLM 接入（不配置则退回本地抽取式总结）
  store.py          SQLite 存取与 FTS5 全文检索
  metrics.py        指标注册表、采样落库、Prometheus 文本
  logs.py           结构化 JSONL 日志（warn/error 自动进事件表）
  recluster.py      场次结束后用全场声纹重新聚类
  export.py         Markdown / SRT 导出
  web/              前端页面（原生 HTML/JS，无构建步骤）
    *.html          页面骨架：只剩结构与资源引用，不内联 CSS/JS
    css/            样式：index.css / rooms.css 按页拆出
    js/index/       主工作台，15 个 ES Module：session / filters / summary / live / subs …
    js/rooms/       收听页，9 个 ES Module：render / detail / data / canvas …
docs/               设计说明与功能文档
tools/              模型下载、开发热重启、声纹校准与自检、高光精度验收、并发压测
models/             本地模型（不提交）
data/               数据库、录音、日志（不提交）
```

前端没有打包器：浏览器直接加载原生 ES Module（`<script type="module" src="/static/...">`），
服务端 `/static/*` 原样返回。改一个交互只需要打开对应功能域的那个文件，模块之间的依赖
写在文件头的 `import` 里。拆分原则与取舍见 `docs/11-frontend-refactor-options.md`。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `LIVENOTES_HOST` / `LIVENOTES_PORT` | `127.0.0.1` / `8765` | 监听地址与端口 |
| `LIVENOTES_MODELS_DIR` | `./models` | 模型目录 |
| `LIVENOTES_DATA_DIR` | `./data` | 数据库/录音目录（压测可指向临时目录） |
| `LIVENOTES_MAX_SPEAKERS` | `8` | 实时阶段声纹聚类上限 |
| `LIVENOTES_ASR_WORKERS` | 自动 | ASR 调度/解码线程数；默认按 CPU 核数（2~8） |
| `LIVENOTES_ASR_THREADS` | 自动 | 单次解码的 ONNX Runtime intra-op 线程数（1~4） |
| `LIVENOTES_METRICS_SAMPLE_S` | `15` | 指标快照落库间隔（秒，最小 5） |
| `LIVENOTES_METRICS_RETENTION_DAYS` | `7` | `metrics_samples` / `metric_events` 保留天数 |
| `LIVENOTES_STALL_S` | `60` | 有语音但多久没有新定稿算停滞 |
| `LIVENOTES_STALL_IDLE_S` | `30` | 录内静默超过多久就不算停滞 |
| `LIVENOTES_LLM_PRICE_INPUT` / `_OUTPUT` | `0` | 每百万 token 单价，用于看板折算花费 |
| `LIVENOTES_ASR_MAX_PENDING_FINALS` | `8` | 单会话定稿排队上限，超出丢最旧并计入未转写 |
| `LIVENOTES_PARTIAL_TTL_S` | `2.5` | 临时字幕排队超过该秒数直接丢弃 |
| `LIVENOTES_PUSH_COOLDOWN` / `_SESSION_CAP` / `_CAP` / `_DIGEST` / `_CARD` | 600 / 3 / 5 / 1 / 1 | 飞书推送节奏与卡片，见 [docs/08](docs/08-feishu-push-time.md) |
| `FEISHU_OWNER_OPEN_ID` / `FEISHU_CHAT_IDS` | 空 | 主人 open_id；群聊白名单 `chat_id`（逗号分隔） |
| `FEISHU_NL_ENABLED` | `0` | 1 = 白名单群里 `@机器人` 可用自然语言；总结仍需确认 |
| `LIVENOTES_BRIEF_LLM` / `_DAILY_CAP` / `_SESSION_TOKEN_CAP` | 1 / 30 / 30000 | 简报模型开关、每日调用上限、单场 token 软上限 |
| `LIVENOTES_BRIEF_TOPICS_COOLDOWN` / `_SUMMARY_COOLDOWN` | 60 / 120 | 同一房间话题/总结的最短间隔（秒） |
| `LIVENOTES_BRIEF_SUMMARY_MAX_ROWS` / `_MAX_MINUTES` | 350 / 120 | 总结最多送模型多少句、`/近聊` 窗口上限 |
| `LIVENOTES_LLM_BASE` / `_KEY` / `_MODEL` | 空 | 可选 LLM 配置 |
| `LIVENOTES_LLM_CONCURRENCY` | `2` | 全局同时打模型的请求上限（1~16） |
| `LIVENOTES_LLM_MAX_ATTEMPTS` / `_BACKOFF_BASE` / `_BACKOFF_MAX` | `3` / `1.0` / `30` | 429/5xx/网络错误的尝试次数与指数退避（秒） |
| `LIVENOTES_LLM_SESSION_TOKEN_BUDGET` | `0` | 单场 token 预算，`0` = 不限（默认） |
| `LIVENOTES_LLM_DAILY_TOKEN_BUDGET` | `0` | 全局每日 token 预算，`0` = 不限（默认） |
| `LIVENOTES_LLM_DEDUPE_TTL` / `_TASK_TIMEOUT` | `600` / `600` | 成功结果复用窗口、HTTP 等任务的上限（秒） |
| `LIVENOTES_NO_BROWSER` | 空 | 设为 `1` 不自动开浏览器 |
| `LIVENOTES_MAX_ROOMS` | `5` | 同时监听几路（独立浏览器约 0.9GB/路；共享模式 3 路实测浏览器 2.4GB） |
| `LIVENOTES_CAPTURE_MODE` | `per_room` | 采集底座：`per_room` = 每个房间一个浏览器；`shared` = 一个 Edge 开多个房间标签页（省内存，实验性）；`direct` = 从抖音 reflow 页取流、ffmpeg 直取（最省内存；失败停下等你确认，不自动切浏览器） |
| `LIVENOTES_DIRECT_FALLBACK` | 空（用设置页） | direct 失败后怎么办：`ask`（默认，停在收听页等你点「切换浏览器采集」）/ `auto`（旧行为，自动切 `per_room`，不可用再 `shared`）/ `per_room`、`shared`（自动切到指定引擎）/ `none`（只报失败，不提供切换）。平时在「⚙ 设置 → 采集」里选，改完立即生效；这里设了环境变量则覆盖设置页，留给脚本/临时调试 |
| `LIVENOTES_SHARED_BROWSER` | 空 | 共享模式指定 Edge / Chrome for Testing 的 `exe` 路径；不填按 Edge → Chrome for Testing 顺序找 |
| `LIVENOTES_CONFIRM_TTL` | `600` | 陌生站点的确认卡多久失效（秒） |
| `LIVENOTES_ALLOW_LOCAL` | 空 | 设为 `1` 才允许本机/局域网链接（调试用） |
| `LIVENOTES_FFMPEG` | 空 | 指定 ffmpeg 路径；不填走 PATH（转码与片段切分要用） |
| `LIVENOTES_AUDIO_AAC_BITRATE` | `32k` | 归档 m4a 码率 |
| `LIVENOTES_AUDIO_OPUS_BITRATE` | `24k` | 飞书语音片段 opus 码率 |
| `LIVENOTES_WAV_RETENTION_DAYS` | `7` | 原 WAV 保留天数，`0` = 不删 |
| `LIVENOTES_AUDIO_RETENTION_DAYS` | `0` | 归档 m4a 保留天数，`0` = 永久 |

## 并发与性能

采集/VAD 与 ASR 已解耦：每个会话的采集线程只负责断句和入队，解码由共享的
`app/asr_pool.py` 工作池完成（识别器单例、线程安全、CPU 上并发 decode）。定稿优先于
临时字幕；同一会话定稿严格按顺序；跨会话轮转；临时字幕每会话只保留最新一帧并带 TTL。

```bash
# 调度器自测（不需要模型，几秒）
python tools/selftest_asr_pool.py

# 1/3/5/10 路 × 60 秒稳定态压测（RTF / P50/P95 / 丢块率 / CPU / 内存）
python tools/stress_live.py --tracks 1,3,5,10 --duration 60

# 正式 2 小时长稳（4 档共约 8 小时，建议放后台）
python tools/stress_live.py --tracks 1,3,5,10 --duration 7200 --warmup 60 \
    --json data/asr-soak-2h.json --md data/asr-soak-2h.md
```

实测（12 核 Windows，60 秒稳定态）：1 路定稿 P95 0.86s；10 路 P95 0.91s（1.06 倍），
定稿丢句率 0%，PCM 丢块率 0%，CPU 平均 4.2 核。10 路 × 10 分钟长稳：P95 0.90s，
同口径单路 P95 0.84s（1.07 倍），1640 句 0 丢句，CPU 平均 4.3 核，RSS 1105→1166MB。
2 小时长稳尚未运行，命令见文档。详见
[`docs/07-asr-concurrency.md`](docs/07-asr-concurrency.md)。

## 已知边界

观众端只能拿到平台下混的一路音频。8~10 人**同时**发言时，从单路混音里稳定分离出每个人并归属身份没有可工程化的解。本原型对重叠段采取「标记 + 不转写 + 不进总结」，宁可留白不编造；真正打通需要主播端 / RTC 的 per-uid 分轨（见 `docs/02-live-tab-capture.md`）。

## 文档

- [`docs/02-live-tab-capture.md`](docs/02-live-tab-capture.md) 实时监听与标签页采集全流程
- [`docs/03-dev-reload.md`](docs/03-dev-reload.md) 开发热重启
- [`docs/04-llm-settings.md`](docs/04-llm-settings.md) LLM 配置
- [`docs/05-ai-title.md`](docs/05-ai-title.md) AI 标题
- [`docs/05-voiceprint.md`](docs/05-voiceprint.md) 声纹与说话人
- [`docs/08-feishu-push-time.md`](docs/08-feishu-push-time.md) 飞书推送：时间与时机的取舍
- [`docs/09-edit-glossary.md`](docs/09-edit-glossary.md) 校对与术语：改字、改判、合并说话人
- [`docs/10-llm-task-scheduler.md`](docs/10-llm-task-scheduler.md) LLM 全局调度：并发、退避、预算与幂等
- [`docs/14-feishu-live-brief.md`](docs/14-feishu-live-brief.md) 飞书直播简报：状态 / 近10分钟 / 阶段性总结的命令、卡片与接口清单
- [`docs/15-live-audio-listen.md`](docs/15-live-audio-listen.md) 采集中「听原声」：网页回听、飞书语音片段与连续旁听
- [`docs/16-recording-sharing-options.md`](docs/16-recording-sharing-options.md) 录音内容对外分享：五种方案、对比矩阵与选型
- [`docs/17-capture-lifecycle.md`](docs/17-capture-lifecycle.md) 采集生命周期与边界条件：停止、重启、断流、代理接管
- [`docs/21-desktop-diagnostics.md`](docs/21-desktop-diagnostics.md) 桌面故障诊断：托盘提示、日志打包与一键导出
- [`docs/22-desktop-usage-guide.md`](docs/22-desktop-usage-guide.md) 桌面版使用教程：开发态运行、打包安装、托盘与数据目录
- [`docs/23-desktop-ux-redesign.md`](docs/23-desktop-ux-redesign.md) 桌面交互重构：从多窗口到单窗口工作区（方案对比与路线）
- [`docs/24-m1-spike-report.md`](docs/24-m1-spike-report.md) M1 单窗口壳 Spike：主窗口 IPC、子视图挂载与 z 序
- [`docs/25-desktop-workspace-implementation.md`](docs/25-desktop-workspace-implementation.md) 单窗口工作区 M1–M4：实现、验收与已知限制
- [`docs/26-ui-theme-b.md`](docs/26-ui-theme-b.md) Studio / Paper 主题：配色、窗口按钮与验收截图
- [`docs/27-process-lifecycle.md`](docs/27-process-lifecycle.md) 进程生命周期：关窗语义、Job Object、单实例接管与开发态回收

## 许可证

[MIT](LICENSE)。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
