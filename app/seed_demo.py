# -*- coding: utf-8 -*-
"""生成一场 2 小时、8~10 人上麦的演示场次。

刻意做成「混音兜底」的真实形态：说话人是聚类标签（仅部分人工确认）、
存在重叠不可信片段、存在冷场未转写区间、按时间段话题有明显分布差异。
这样按时段筛选 / 按分类筛选 / 降权 / 覆盖率提示 都能被真实检验，而不是空跑。
"""
from __future__ import annotations

import random
import sqlite3

from .analysis import classify

SESSION_ID = "DEMO_20261104_2000"
DURATION = 7200.0

SPEAKERS = [
    {"speaker_id": "SPK_00", "display_name": "主播·阿哲",   "verified": 1, "note": "主播端确认"},
    {"speaker_id": "SPK_01", "display_name": "小鹿",         "verified": 1, "note": "上麦自报昵称"},
    {"speaker_id": "SPK_02", "display_name": "聚类标签 02",  "verified": 0, "note": "身份未确认"},
    {"speaker_id": "SPK_03", "display_name": "Mia",          "verified": 1, "note": "上麦自报昵称"},
    {"speaker_id": "SPK_04", "display_name": "聚类标签 04",  "verified": 0, "note": "疑似与 07 为同一人，待合并"},
    {"speaker_id": "SPK_05", "display_name": "聚类标签 05",  "verified": 0, "note": "身份未确认"},
    {"speaker_id": "SPK_06", "display_name": "聚类标签 06",  "verified": 0, "note": "身份未确认"},
    {"speaker_id": "SPK_07", "display_name": "聚类标签 07",  "verified": 0, "note": "疑似与 04 为同一人，待合并"},
    {"speaker_id": "SPK_08", "display_name": "聚类标签 08",  "verified": 0, "note": "仅发言 2 次"},
    {"speaker_id": "SPK_09", "display_name": "聚类标签 09",  "verified": 0, "note": "身份未确认"},
]

WORK = [
    "我做了四年后端，现在拿到一个涨薪百分之三十的 offer，但是要去外地，纠结要不要接。",
    "建议你先看三件事：现金流、岗位实际的汇报线、还有试用期条款，别只盯着薪资数字。",
    "我之前跳过一次，最大的坑是没问清楚团队最近半年的离职率，进去三个月就重组了。",
    "面试官问期望薪资的时候，我一般先反问他们的预算区间，把锚点交回去，成功率会高不少。",
    "简历里别写负责了什么，写你做成的结果，最好带一个能核对的数字，比如延迟降了多少。",
    "外包转甲方确实难，但可以先把项目沉淀成两三个能复述的案例，面试时讲细节。",
    "试用期被劝退这种情况，记得保留书面沟通记录和考勤，口头说的一律不算数。",
    "别急着裸辞，先算清楚手上的现金流能撑几个月，低于六个月我会建议先谈内部转岗。",
    "我补充一个点，跨城市跳槽要把租房和通勤成本折进去，实际到手可能比现在还少。",
    "面试最后问「团队现在最头疼的问题是什么」，对方怎么答基本能看出真实情况。",
    "我见过最可惜的是能力不错但简历全是名词，没有一条结果，连筛选都过不了。",
    "如果你现在只是因为跟领导不合想走，那换个公司大概率还会遇到同样的问题。",
    "offer 里的签字费和年终口径一定要写进合同，只口头承诺的后面基本都会打折。",
    "三十五岁焦虑被说得太重了，我这边招人看的是能不能独立闭环一件事，不是年龄。",
    "谈薪的时候把当前薪资证明准备好，别虚报太多，背调过不了反而丢掉机会。",
    "我建议先做一次模拟面试录音回听，很多人不是不会，是讲不清楚自己做过什么。",
]

EMOTION = [
    "相处两年了，对方一直不想聊结婚，我不知道该不该继续等下去。",
    "我觉得这不是时间问题，是你们对关系的定义不一致，得摊开谈一次，别靠猜。",
    "冷暴力比吵架更消耗人，先设一个明确的沟通底线，比如多久之内必须回应。",
    "异地三年最后还是散了，问题不在距离，是我们谁都不肯先说自己的计划。",
    "相亲遇到一个条件都合适但完全聊不动的，家里一直催，我有点顶不住了。",
    "别用「他应该懂我」来判断关系，把需求直接说出来，能接住的人才值得继续。",
    "分手后还保持联系，其实是把决定一直往后拖，双方都没法真的往前走。",
    "我自己的经验是先看冲突之后怎么处理，比看平时对你多好准得多。",
    "对方总说忙，但朋友圈一直在更新，这种信号其实已经很清楚了。",
    "如果一段关系让你长期睡不好、反复自我怀疑，那本身就是一个需要认真对待的答案。",
    "你们可以把「什么时候、要不要、怎么安排」拆成三个问题分开谈，别搅在一起。",
    "我以前也觉得再等等就好了，后来发现等的是对方给一个他根本不想给的答案。",
    "家里催婚的压力要自己先接住，别把它转嫁成你对伴侣的要求。",
    "沟通里最有用的一句是「我听到你的意思是……对吗」，能省掉一大半误会。",
]

SIDE = [
    "现在做自媒体还有机会吗，我只有晚上两小时，能投入的时间很有限。",
    "别一上来就想变现，先固定一个细分选题做满三十条再复盘数据，看留存曲线。",
    "接单平台抽成高，但前期能省掉获客成本，把账算清楚再决定要不要长期待。",
    "账号突然限流，先查违规通知，再看近七天的完播曲线，别急着删内容。",
    "副业最大的成本不是时间，是切换成本，一天切四件事基本什么都做不深。",
    "我做了半年才第一笔收入，前面全是在练选题，这个预期得先摆正。",
    "房东不退押金，合同里写的是提前三十天通知，我提前了二十天，还有办法吗。",
    "先拍照留证，再走平台申诉，别在群里吵，情绪解决不了押金的事。",
    "租房一定拍入住视频，水电表读数也拍一遍，退租时能省掉很多扯皮。",
    "通勤单程超过一小时，我会把它折算成房租差价，很多时候搬近一点更划算。",
    "报价别按工时算，按交付物算，不然你效率越高挣得越少。",
    "内容违规大多数不是标题问题，是素材授权没弄清楚，这块一定要留底。",
    "先把一个平台的规则吃透再铺第二个，同时铺五个基本都会违规。",
    "副业收入不稳定，建议单独留一笔税钱，别到年底才发现要补。",
]

CLOSING = [
    "今天就到这儿，最后把三个高频问题再收一下，大家记一下时间点回头看回放。",
    "第一条，跳槽先看现金流和汇报线，别只盯薪资；第二条，简历写结果不写职责。",
    "关系那块最核心的一句：把需求说出来，别让对方猜，冲突之后的处理方式最说明问题。",
    "副业别急着变现，先做满三十条再复盘，账号限流先查违规通知和完播曲线。",
    "今天没连上麦的家人别急，明天同一时间我们继续，麦序会在开播前发。",
    "感谢几位连麦的朋友，愿意把具体情况讲出来，比泛泛问一个问题有用得多。",
]

HOST = [
    "欢迎来到直播间，咱们今天还是老规矩，一个一个上麦，别抢麦啊。",
    "下一位，麦序我念一下，稍等别急。",
    "有请这位家人，把麦打开，声音稍微大一点。",
    "感谢刚才这位的分享，咱们扣个 1 鼓励一下。",
    "稍等啊，这边连一下，网络有点卡。",
    "没点关注的点个关注，咱们继续下一个。",
    "来，把你的情况简单说一下，别一上来就讲十分钟背景。",
    "上麦的家人先把耳机戴上，不然会有回声。",
]
QUESTION = [
    "请问主播，我这个情况能连麦吗，想具体问一下。",
    "老师我想问一下，简历投了五十份没回音，是不是方向错了？",
    "有没有适合新手的入门路线，能简单说两句吗？",
    "我能不能插个队，赶着下班，问题不长。",
    "想咨询一下，这种情况一般要准备多久啊？",
    "主播刚才那点没听清，能再讲一遍吗？",
    "请问这个适用于应届生吗，我刚毕业。",
]
CHITCHAT = [
    "哈哈哈哈哈哈", "在吗在吗", "晚上好家人们", "我刚下班，饿死了", "有人吗，好冷清",
    "点首歌呗", "主播声音好好听", "来啦来啦", "水一下", "困了困了", "嘿嘿",
    "路过打个卡", "今天好热啊", "嗯", "对对对",
]
AD = [
    "有需要资料的加我微信，备注直播间，免费发你一份简历模板。",
    "小黄车一号链接今天有优惠券，扣 1 我发大家。",
    "我们这个课现在报名立减三百，私信我发你试听，名额不多。",
    "关注一下不迷路，明天同一时间开播，还有福利。",
    "代做简历优化，报价私信，走平台担保，三天交付。",
    "需要的家人扫码进群，资料包我统一发，别在公屏刷了。",
]

ACTS = [
    (0.0, 2100.0, "work", WORK),
    (2100.0, 4200.0, "emotion", EMOTION),
    (4200.0, 6300.0, "side", SIDE),
    (6300.0, 7200.0, "closing", CLOSING),
]

PREFIX = ["", "", "", "", "嗯，", "就是说，", "我觉得吧，", "对，", "然后"]
SUFFIX = ["", "", "", "", "吧。", "这样。", "你们懂我意思吧。", "仅供参考啊。"]


def _vary(text: str, rng: random.Random) -> str:
    pre = rng.choice(PREFIX)
    suf = rng.choice(SUFFIX) if text[-1] in "。！？" else ""
    body = text[:-1] + "，" if suf and text.endswith("。") else text
    return (pre + body + suf).strip()


def build_rows() -> tuple[list[dict], float, float]:
    rng = random.Random(20261104)
    rows: list[dict] = []
    t = 8.0
    turn = 0
    overlap_s = 0.0
    untranscribed_s = 0.0
    guest_weights = [3, 2, 3, 1, 2, 2, 1, 1, 3]
    guest_ids = [s["speaker_id"] for s in SPEAKERS[1:]]

    while t < DURATION - 30:
        act = next((a for a in ACTS if a[0] <= t < a[1]), ACTS[-1])
        roll = rng.random()
        if roll < 0.10:
            text, pool = rng.choice(HOST), "host"
        elif roll < 0.20:
            text, pool = rng.choice(QUESTION), "question"
        elif roll < 0.30:
            text, pool = rng.choice(CHITCHAT), "chat"
        elif roll < 0.36:
            text, pool = rng.choice(AD), "ad"
        else:
            text, pool = rng.choice(act[3]), "substantive"

        if pool == "substantive":
            text = _vary(text, rng)
        spk = SPEAKERS[0]["speaker_id"] if pool == "host" else rng.choices(guest_ids, weights=guest_weights)[0]
        label = next(s["display_name"] for s in SPEAKERS if s["speaker_id"] == spk)
        dur = min(22.0, max(3.0, len(text) * 0.20 + rng.uniform(2, 6)))
        conf = round(rng.uniform(0.82, 0.99), 3)
        c = classify(text, False, conf)
        rows.append(dict(session_id=SESSION_ID, t_start=round(t, 1),
                         t_end=round(t + dur, 1), speaker_id=spk, speaker_label=label,
                         text=text, asr_conf=conf, overlap=0, **c))

        # 约 10% 的回合插入「多人抢话」：同一时间 2~3 个人，ASR 不可信
        if rng.random() < 0.10 and pool != "host":
            for k in range(rng.choice([1, 1, 2])):
                other = rng.choice([g for g in guest_ids if g != spk])
                olabel = next(s["display_name"] for s in SPEAKERS if s["speaker_id"] == other)
                st = round(t + rng.uniform(0.5, 3.0), 1)
                od = round(rng.uniform(3.0, 7.0), 1)
                frag = rng.choice(WORK + EMOTION + SIDE + CHITCHAT)[:16]
                otext = f"（多人重叠·转写不可信）{frag}……"
                oc = round(rng.uniform(0.12, 0.38), 3)
                cc = classify(otext, True, oc)
                rows.append(dict(session_id=SESSION_ID, t_start=st, t_end=round(st + od, 1),
                                 speaker_id=other, speaker_label=olabel, text=otext,
                                 asr_conf=oc, overlap=1, **cc))
                overlap_s += od
        t += dur + rng.uniform(3, 14)
        turn += 1
        if turn % 27 == 0:  # 冷场 / BGM / 音乐段落
            gap = rng.uniform(55, 115)
            untranscribed_s += gap
            t += gap
    return rows, overlap_s, untranscribed_s


def seed(conn: sqlite3.Connection) -> int:
    from . import store
    rows, overlap_s, untranscribed_s = build_rows()
    store.insert_session(conn, {
        "id": SESSION_ID,
        "title": "抖音语音直播间 · 职场 / 情感连麦答疑（演示场）",
        "started_at": "2026-11-04T20:00:00",
        "duration_s": DURATION,
        "source_mode": "demo",
        "sample_rate": 16000,
        "audio_path": None,
        "overlap_seconds": round(overlap_s, 1),
        "untranscribed_seconds": round(untranscribed_s, 1),
        "warnings": [
            "演示数据：说话人标签来自聚类，仅 3 位已人工确认身份，其余为 SPK_0x 占位。",
            "8~10 人同时上麦时观众端只有一路混音，重叠片段已标记为不可信并从总结剔除。",
            "真实系统请优先接入 RTC 分轨（per_uid）；混音路线的精度上限见 docs/01。",
        ],
        "speakers": SPEAKERS,
    })
    n = len(store.insert_utterances(conn, rows))
    print(f"[seed] 场次 {SESSION_ID}：{n} 条发言，"
          f"重叠 {overlap_s:.0f}s，未转写 {untranscribed_s:.0f}s")
    return n
