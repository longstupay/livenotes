# -*- coding: utf-8 -*-
"""领域模型与常量。数据库行统一转成 dict 供 API 层使用。"""
from __future__ import annotations

from collections import OrderedDict

# code -> (显示名, 颜色, 默认价值档, 说明)
CATEGORY_META = OrderedDict([
    ("substantive",      ("实质讨论",   "#2b5cff", "high",   "有观点/信息量，总结的主素材")),
    ("question_consult", ("上麦咨询",   "#d48806", "normal", "提问本身信息量低，但可作为话题锚点")),
    ("host_control",     ("主持控场",   "#7c3aed", "low",    "麦序/欢迎/点赞等流程话术")),
    ("chitchat",         ("闲聊",       "#8c8c8c", "low",    "无营养话题、寒暄、水麦")),
    ("ad",               ("广告引流",   "#e5484d", "low",    "导流、报价、下单、加好友")),
])

VALUE_TAG_META = OrderedDict([
    ("high",   ("高价值", 1.00)),
    ("normal", ("常规",   0.60)),
    ("low",    ("低价值", 0.15)),
])

SOURCE_MODE_META = OrderedDict([
    ("per_uid", ("分轨录制（RTC 单流/主播端多轨）", "说话人由 uid 直接确定，无重叠问题，可达会议记录级质量")),
    ("mixed",   ("混音兜底（本机回环录音）",       "说话人为聚类标签，重叠段不转写，需人工校正")),
    ("direct",  ("直取直播流（ffmpeg）",           "解析流地址后直接拉音频，不加载播放器页面；失败自动回退浏览器")),
    ("tab",     ("标签页实时采集",                 "只抓被选中标签页的音频，声纹在线聚类，1~3 人轮流说话可用")),
    ("demo",    ("演示数据",                       "用于验证下游功能，非真实采集")),
])


def category_label(code: str) -> str:
    return CATEGORY_META.get(code, ("未分类",))[0]


def fmt_ts(seconds: float, with_hours: bool = True) -> str:
    """秒 -> HH:MM:SS / MM:SS，供时间码显示。"""
    s = max(0, int(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}" if with_hours else f"{m:02d}:{sec:02d}"


def parse_ts(text: str) -> float:
    """HH:MM:SS / MM:SS -> 秒。解析失败返回 0。"""
    try:
        parts = [int(p) for p in str(text).strip().split(":")]
    except (ValueError, AttributeError):
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return float(parts[0]) if parts else 0.0
