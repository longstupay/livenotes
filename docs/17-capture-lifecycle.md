# 17 采集生命周期与边界条件

这份文档回答一个问题：**一次采集从「加进来」到「停下来」，每一步是谁在写状态、
进程挂了/重启了会怎样、为什么系统不会自己把直播间再打开一遍。**

2026-11 的事故：用户点了停止，采集还在继续；重启服务后，上次的直播间又自己
打开了。两个 bug 的根因是同一个——把「进程还活着」当成了房间状态，又让自动恢复
压过了用户意图。修完之后，规则收敛成下面三条。

## 1. 三个参与者

| 参与者 | 进程 | 负责 | 不负责 |
|---|---|---|---|
| 本地服务 | `run.py` / `app/server.py` | 房间状态、会话内存、DB、看门狗、启动收口 | 不直接拉浏览器/ffmpeg |
| 采集代理 | `tools/capture_agent.py` | 每 ~3 秒轮询 `/api/rooms`，按状态拉起/回收直取或浏览器采集 | 不决定房间该不该跑 |
| 采集页 | 浏览器里的标签页 / ffmpeg | 取音频、往 `/api/live/feed` 送块、上报启动阶段 | 不写房间终态 |

**房间该不该跑，只由 `rooms.json` 的 `state` 决定；采集代理只执行。**
进程是否活着不写进 `rooms.json`，而是两处现算：

- 服务端的实时会话：`live._SESSIONS`（进程内存，重启即清空）
- 采集端心跳：`/api/capture/status`（服务内存，重启即清空）

## 2. 状态机

```
加房 / 飞书加房
      │
      ▼
  ┌────────┐   代理接单    ┌──────────┐   会话建立/首块入库   ┌──────┐
  │pending │──────────────▶│ starting │──────────────────────▶│ live │
  └────────┘               └──────────┘                       └──────┘
      │                         │ 拉起失败                        │
      │ 用户取消                 ▼                                │ 用户停止
      │                    ┌────────┐                            │ 删除房间
      │                    │ failed │                            │ 看门狗自动收尾
      │                    └────────┘                            │ 服务重启收口
      │                         │ 用户重试                        │
      ▼                         ▼                                ▼
  ┌──────────────────────────────────────────────────────────────────┐
  │ stopped                                                          │
  │   interrupted=true  → 服务重启时收口的「已中断」                   │
  │   auto_stopped=true → 看门狗因断流收口的，可由代理自动重启          │
  └──────────────────────────────────────────────────────────────────┘
      │
      │ 用户点「重试」（清会话/错误/中断标记）
      ▼
   pending
```

- `stopped` 是终态。能离开它的只有两条路：
  1. **用户点「重试」** → `pending`（清 `session_id`、`error`、`interrupted`、
     `auto_stopped`、切换标记）。
  2. **断流自动恢复** → 仅当 `auto_stopped=true` 且原因里含「音频块」、页面探针
     确认还在播、且没超过重启上限；这是看门狗自己停的，不是用户停的。
- 用户停止后，任何迟到的 `starting/live/failed` 上报都会被
  `/api/rooms/state` 丢弃，**包括采集代理自己的上报**；要重新开始必须先置回
  `pending`（用户重试 / 断流自动恢复）。见 `app/server.py` 的 `stopped` 守卫。

## 3. 正常路径（一条采集）

1. 用户粘链接 → `rooms.add`：`pending`，`stage=0`。
2. 代理 tick 看到 `pending` → `starting`，上报 stage 1..5（浏览器/页面/音频/
   会话/入库）。
3. 采集页建立会话、首块音频入库 → `live`。
4. 用户点停止 → `_close_room`：**先**写 `stopped`，**再** `live.stop` 收尾。
   顺序反了，收尾的几十秒里代理会以为房间还在跑，继续送块甚至重建会话。
5. 删除房间同理：先 `rooms.remove`，再停会话。

## 4. 边界条件矩阵

| # | 场景 | 系统行为 | 用户看到 |
|---|---|---|---|
| 1 | 采集中点「停止」 | 先落 `stopped` 再收尾；代理下一拍回收 ffmpeg/浏览器 | 已停止；有录音则「已保存」 |
| 2 | 采集中删除房间 | 先删房间再停会话；代理按「房间已删除」回收 | 卡片消失 |
| 3 | 待启动时取消 | `stopped`，不建会话 | 已停止，无录音 |
| 4 | 采集页/浏览器崩溃，服务还活着 | 房间仍 `live` 但会话丢失；代理回收并重新拉起 | 会话丢失/启动中，自动恢复 |
| 5 | 直取断流，看门狗自动收尾 | `stopped` + `auto_stopped`；代理探针确认页面还在播才自动重启（有上限） | 已停止（自动），短暂后回到采集 |
| 6 | **只重启采集代理**（服务未重启） | 代理启动时 `adopt` 已连着的采集，不重复开浏览器 | 无感 |
| 7 | **只重启服务**（代理还在跑） | **启动收口**：`pending/starting/live` → `stopped` + `interrupted`；代理下一拍回收 | 已中断；点「重试」才重新开始 |
| 8 | 服务和代理一起重启 | 同 7，且不再需要 adopt | 已中断 |
| 9 | 进程被强杀（`.pcm` 遗留） | 启动时 `recover_spools` 先收尾成 WAV 写回 DB，再执行 7 | 已中断，录音已保存 |
| 10 | 启动到一半崩溃（`starting`，无会话） | 收口成 `stopped` + `interrupted`，提示「还没建立会话」 | 已中断，可重试 |
| 11 | 房间本来就是 `failed` 后重启 | 保持 `failed`，不自动拉起 | 失败；详情里可重试/切换 |
| 12 | 房间已经 `stopped` 后重启 | 原样保留；用户停的就是用户停的 | 已停止 |
| 13 | 多路同时采集 | 每路独立状态、独立会话；收口按房间逐个处理 | 每路各自「已中断」 |
| 14 | 飞书机器人加房后马上重启 | 与普通加房一致：落 `pending`，启动时若还没被接单则收口 | 已中断，点「重试」 |
| 15 | 停止后采集页/代理迟到的上报 | 任何来源的 `starting/live/failed` 都被忽略 | 状态不会被点亮 |
| 16 | 无人值守，需要重启后自动续采 | 设 `LIVENOTES_RESUME_ROOMS=1`，跳过启动收口 | 自动恢复（显式选择） |

### 为什么默认不自动续采

自动续采要相信一份「上次进程还活着」的状态。但进程已经换了，浏览器/ffmpeg 可能
还在，也可能已经死了；更重要的是，**重启服务本身就是一个显式动作**。自动重开
直播间的代价（隐私、带宽、一场 1.4GB 的录音）远高于让用户点一次「重试」。

所以默认规则只有一句：**进程重启 = 所有没正常结束的房间作废为「已中断」，
重新开始必须由人点「重试」。** 需要无人值守的机器用环境变量显式打开。

## 5. 三条不变量

1. **显式意图优先于自动恢复。** 停止、删除、重启都算用户意图；自动恢复只能
   发生在「服务还活着、房间还在 active」的时候，不能跨进程复活。
2. **落状态先于收尾。** 停止/删除先把终态写进 `rooms.json`，再去做可能几十秒
   的收尾；采集端只认状态，下一拍就会停。
3. **`rooms.json` 不记录进程存活。** 进程活没活由实时会话和采集心跳现算；
   重启后它们自然清空，不会留下「看起来还在跑」的化石。

## 6. 代码位置

| 规则 | 位置 |
|---|---|
| 状态字段与持久化 | `app/rooms.py`（`add` / `update` / `list_rooms`） |
| 停止顺序、收口 | `app/server.py`（`_close_room` / `_mark_room_stopped` / `reconcile_rooms`） |
| 启动调用顺序 | `run.py`（`recover_spools` → `reconcile_rooms` → `make_server`） |
| 自动续采开关 | `app/config.py`（`RESUME_ROOMS`） |
| 代理接管/回收 | `tools/capture_agent.py`（`adopt` / `tick` / `restart_after_loss`） |
| 直取重建会话前的状态检查 | `tools/direct_capture.py`（`_renew_session`） |
| 停止/重试按钮 | `app/web/js/rooms/actions.js`、`render.js`、`detail.js` |

**顺序要求：`reconcile_rooms` 必须在 `make_server` 之前执行。** 否则采集代理
可能先拿到 HTTP 响应，把残留的 `live/starting` 当成待办，抢在收口前重新拉起。

## 7. 测试

- `tests/test_rooms.py::test_close_room_marks_stopped_before_stopping_session`
  —— 停止先落状态。
- `tests/test_rooms.py::test_reconcile_rooms_demotes_active_at_startup`
  —— 启动收口把 `pending/starting/live` 变成 `interrupted`，保留会话与录音，
  不碰 `stopped/failed`，且幂等。
- `tests/test_rooms.py::test_reconcile_skips_rooms_without_session_and_marks_no_recording`
  —— 没建会话的房间收口后明确「无录音」，重试可清掉中断标记。
- `tests/test_direct_capture.py::test_renew_session_skipped_when_room_stopped`
  —— 直取代理看到 `stopped` 不再重建会话。
- `tests/test_feishu_share.py` —— 分享失败的重试卡与冷却退还。

## 8. 已知取舍

- **启动收口不区分「正常关服务」和「崩溃」**：两种情况下录音都被中断了，统一
  显示「已中断」是诚实的。区别只在 `recover_spools` 是否捞回了 `.pcm`。
- **收口发生在服务启动时，不在退出时**：退出时收尾可能来不及把 WAV 写回 DB，
  `has_recording` 会算错；放到下一次启动、`recover_spools` 之后才算得准。
- **一个房间在 `starting` 时重启，可能丢掉刚打开的浏览器**：代理下一拍看到
  `stopped` 会回收它；损失的是几秒启动时间，换来的是不会有两个进程抢同一路。
