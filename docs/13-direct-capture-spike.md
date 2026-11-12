# Direct 采集引擎可行性 Spike

> 分支：`spike/direct-capture`（基于 `fix/live-capture-self-healing`）
>
> 日期：2026-11-08
>
> 结论：**GO（B 站、抖音）/ 条件 GO（快手）**。direct 模式采集侧每路约 28~38MB、CPU 0.4~1.8%，5 路并发合计 139MB / 3.1%；比浏览器每路 0.5~0.9GB 低一个数量级；建议进入 Phase 1，浏览器保留兜底。
>
> **Phase 1 已落地（2026-11-08）**：只实现抖音，入口 `LIVENOTES_CAPTURE_MODE=direct`，
> 代码在 `tools/direct_capture.py`。解析改为抓 reflow 页里的现成播放地址（streamlink 可选兜底）；
> 失败默认停在「待切换」，用户在收听页确认后才走浏览器（设置页「采集」里可选 `ask` /
> `auto` / `none`，默认 `ask`；环境变量 `LIVENOTES_DIRECT_FALLBACK` 可覆盖），
> 不再无感回退。B 站、快手暂不实现。

## 1. 目标

验证「不加载直播页面，直接解析流地址 + ffmpeg 拉音频」能否稳定替代或补充 Chromium 采集：

- 3 个平台 × 5 个在播房间，测解析成功率与首音频延迟；
- 每个房间 30 秒，测分块连续性、ffmpeg 内存/CPU；
- 每平台挑 1 路跑 10 分钟，测长稳、断流、内存增长；
- 5 路并发 60 秒，测聚合内存/CPU 与并发稳定性；
- 记录 URL 类型、过期时间、登录/Cookie 要求；
- 给出 Phase 1 的 go/no-go 与风险清单。

## 2. 环境与方法

| 项 | 值 |
|---|---|
| 机器 | Windows 10 Pro 19045，16GB |
| Python | 3.13.13 |
| ffmpeg | 9.0-full_build-www.gyan.dev |
| yt-dlp | 2026.10.26 |
| streamlink | 8.6.3 |
| 浏览器 | 仅用于发现房间；采集阶段无浏览器进程（实测 `msedge` 进程数 0） |

流程：

```
直播房间 URL
  -> resolver（yt-dlp / streamlink / 平台 API）
  -> HLS/FLV 地址
  -> ffmpeg -vn -ac 1 -ar 16000 -f s16le pipe
  -> 统计首音频、分块间隔、RSS、CPU
```

房间来源：B 站用公开推荐 API；抖音/快手用共享 Edge 打开直播首页抓当前在播链接。每个平台取前 5 个能解析成功的房间，连续跑 30 秒。

## 3. 解析器矩阵

| 平台 | yt-dlp | streamlink | 自定义 API | 本 spike 采用 | 流类型 | URL 过期 |
|---|---|---|---|---|---|---|
| B 站 | ✅ | ✅ | — | `yt-dlp -f bestaudio/best -g` | HLS（minihevc） | 约 1 小时 |
| 抖音 | ❌ Unsupported URL | ✅ | — | `streamlink --stream-url ... best` | FLV | 约 7 天 |
| 快手 | ❌ Unsupported URL | ❌ 无插件 | ✅ `live_api/gameboard/list` | 自定义 API | FLV | 约 24 小时 |

补充：

- 抖音的 yt-dlp extractor 只覆盖短视频，不覆盖 `live.douyin.com` 直播页；streamlink 的 `douyin` 插件可用。
- 快手的 `livedetail` API 对在播房间也返回 `result=2` / `living=false` 的桩数据，不能用来判断是否在播；`gameboard/list` 返回的 `playUrls` 才是可播放地址。
- 快手的 `gameboard/list` 目前只验证了游戏分类（`gameId=1001` 等）；非游戏分类的公开列表接口尚未打通，这是快手从「条件 GO」转「GO」的主要缺口。

## 4. 30 秒 × 5 房间结果

| 平台 | 成功率 | 解析耗时 | 首音频（中位/范围） | ffmpeg RSS（中位/范围） | CPU（中位/范围） | 最大分块间隔 | >2s 断块 |
|---|---|---|---|---|---|---|---|
| B 站 | **5/5** | 2.4~2.9s | 2.49s / 2.03~3.47s | 33.2MB / 30.6~34.2MB | 1.7% / 1.4~1.8% | 1.86s | 0 |
| 抖音 | **5/5** | 2.6~3.9s | 1.47s / 0.27~4.65s | 28.0MB / 27.9~31.5MB | 0.8% / 0.6~1.1% | 1.24s | 0 |
| 快手 | **5/5** | 0.65~0.90s（API） | 0.52s / 0.44~1.18s | 28.0MB / 28.0~28.1MB | 0.8% / 0.5~0.9% | 0.56s | 0 |

说明：

- 首音频包含 resolver + ffmpeg 启动 + 等待第一个直播分片；抖音最慢 4.65s 是单个房间的冷启动，不是常态。
- 30 秒内没有出现 >2 秒的断块；1~2 秒的间隔主要来自 HLS 分片边界和读取缓冲。
- 快手 5 路全部来自 `gameboard/list` 的游戏分类，URL 是 FLV，ffmpeg 直接拉流，不需要页面。

## 5. 10 分钟长稳

每平台 1 路，600 秒：

| 平台 | 实际音频 | 首音频 | 分块数 | 最大间隔 | >2s 断块 | RSS 峰值 | CPU 均值 | ffmpeg stderr |
|---|---|---|---|---|---|---|---|---|
| B 站 | 596.7s / 600s（99.5%） | 2.16s | 6259 | 1.92s | 0 | 37.8MB | 1.4% | 空 |
| 抖音 | 598.9s / 600s（99.8%） | 1.60s | 25481 | 0.34s | 0 | 28.1MB | 0.7% | 空 |
| 快手 | 599.5s / 600s（99.9%） | 0.43s | 12809 | 0.40s | 0 | 28.4MB | 0.5% | 空 |

- 10 分钟内 ffmpeg 没有退出、没有 >2 秒断块；RSS 相比 30 秒测试只涨 0~4.6MB（B 站 33.2→37.8MB），没有持续增长迹象。
- 三路合计音频 1795.1s / 1800s，缺口来自启动等待和到点终止，不是中途断流。
- `ffmpeg_rc=1` 是测试脚本到点 `terminate()` 的正常结果，stderr 为空。
- 分块数差异来自容器：B 站 HLS 平均每次 `read()` 约 3.0KB，抖音 FLV 约 0.75KB，快手约 1.5KB。Phase 1 不能把 `read()` 返回直接当一块，必须按 4096 样本重新缓冲后再 POST。

## 6. 内存对比

| 方案 | 5 路采集侧内存 | 说明 |
|---|---|---|
| 独立浏览器（现状） | ~4.6GB | 0.93GB/路，含浏览器基础进程 |
| 共享浏览器（已实现） | ~3.5GB（按 3 路实测 2.4GB + 0.56GB/路估算） | 基础进程只付一次 |
| **Direct（本 spike）** | **139.0MB（实测 5 路并发 60 秒）** | 27.6~28.4MB/路 ffmpeg + 短暂 resolver 进程 |

5 路并发实测：5/5 成功，RSS 合计 139.0MB，CPU 合计 3.1%，0 个 >2 秒断块；其中 1 路 `rms≈1e-5`（流本身静音），说明链路正常不代表有声音，健康检查不能只看 chunk 数。

整机口径还要加服务端 ~0.46GB 和 ASR 模型；但采集侧从 GB 级降到百 MB 级，是这次改造最大的收益。

## 7. 风险与边界

1. **URL 过期**：B 站约 1 小时、快手约 24 小时、抖音约 7 天。必须在失败时重新解析；B 站需要按 TTL 主动刷新。
2. **反爬/限流**：本次 5 个房间连续解析均成功；长时间、多路、高频重试可能触发限流。需要退避、缓存解析结果、限制每平台并发。
3. **快手覆盖**：只验证了游戏分类；非游戏直播需要另找列表接口或页面解析，当前应回退浏览器。
4. **抖音解析依赖页面**：streamlink 的 douyin 插件靠页面数据；平台改版会失效，没有 yt-dlp 兜底。
5. **登录/Cookie**：本次房间均无需登录；部分房间可能需要登录态，需要 profile/cookie 管理。
6. **合规**：直取流地址比「浏览器播放 + 本地录音」更容易被平台认定为绕过播放器；上线前需要产品/法务确认。
7. **会话语义**：direct 代理要自己调 `/api/live/start`、`/api/live/chunk`、`/api/live/stop`，并把 `source_mode` 标成 `direct`；当前 `LiveSession.start()` 里写死 `source_mode="tab"`，Phase 1 需要参数化。
8. **音频块契约**：ffmpeg 输出 s16le/16k/mono，按 8192 字节（4096 样本）切块，和现有采集页一致；读取端要做缓冲，避免半块。

## 8. Phase 1 建议

**引擎接口**

```
LIVENOTES_CAPTURE_MODE=direct
  resolve(room_url) -> {stream_url, kind, expires_at}
  ffmpeg supervisor / room -> POST /api/live/chunk
  resolver/ffmpeg 失败 -> 停在待切换，用户确认后才走 per_room/shared（同一个 session）
```

**实现要点**

- `tools/direct_capture.py`（或扩展 `tools/capture_agent.py`）：`DirectAgent`，每个房间一个 ffmpeg 子进程；
- resolver 抽象：B 站走 yt-dlp、抖音走 streamlink、快手走 `gameboard/list`；每个平台独立插件，失败互不影响；
- 健康检查：ffmpeg 退出、10 秒无块、HTTP 403/404 时重新解析；同一 session 重试最多 3 次，再回退浏览器；
- 会话：`/api/live/start` 后把 session 写回 `/api/rooms/state`；`source_mode=direct`；停止时 `/api/live/stop`；
- 依赖：`yt-dlp`、`streamlink` 作为可选依赖，ffmpeg 必须有；写进 `requirements-direct.txt` 和 README；
- 质量选择：只要音频，优先最低视频档（B 站 `high_res-0`/`worst`，抖音 `sd1`/`worst`，快手取 `playUrls` 最低 bitrate），减少带宽；B 站避免 `ultra_high_res`（HEVC）以兼容老 ffmpeg；
- 指标：解析成功率/耗时、首音频、重连次数、URL 过期次数、丢块，进 `/metrics`。

**灰度**

1. B 站、抖音默认 direct；快手游戏分类 direct，其余回退浏览器；
2. 全局开关 `LIVENOTES_CAPTURE_MODE=direct`，失败自动回退 `shared`；
3. 24 小时 × 5 路/平台观察，成功率 ≥95%、无 >5 秒断块、解析器维护 <2 小时/周，再考虑默认开启。

## 9. 复现命令

```bash
# B 站
python -m yt_dlp --no-warnings -f bestaudio/best -g https://live.bilibili.com/10001
# 抖音
python -m streamlink --stream-url https://live.douyin.com/123456789016 best
# 快手
curl -s "https://live.kuaishou.com/live_api/gameboard/list?gameId=1001"
# 拉音频
ffmpeg -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  -i "<stream-url>" -vn -ac 1 -ar 16000 -f s16le -
```

原始数据：`.tmp/spike/{bili,dy,ks}_batch.jsonl`、`*_long.jsonl`、`concurrent.jsonl`（未入库）。
