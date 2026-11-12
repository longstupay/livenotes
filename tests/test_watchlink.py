# -*- coding: utf-8 -*-
"""短链/reflow 的落地判定：不能再把 reflow id 当成直播间 web_rid。"""
from __future__ import annotations

from app import watchlink

REFLOW = "https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000002"


def _resolver(url: str) -> tuple[str, str]:
    return REFLOW + "?did=abc&sec_user_id=xyz&share_id=7700000000000000002", ""


def test_short_link_keeps_reflow_landing_not_fake_live_id():
    d = watchlink.decide("https://v.douyin.com/HHbdNjaE0jU", _resolver)
    assert d["kind"] == "watch", d
    # reflow/<id> 是分享 id，不是 live.douyin.com/<id>；实测前者能播、后者报错
    assert d["url"] == REFLOW
    assert "live.douyin.com/7700000000000000002" not in d["url"]


def test_reflow_direct_drops_tracking_params():
    d = watchlink.decide(REFLOW + "?did=abc&sec_user_id=xyz", None)
    assert d["kind"] == "watch"
    assert d["url"] == REFLOW


def test_canonical_live_room_still_passes():
    d = watchlink.decide("https://live.douyin.com/123456789012?camera_id=0", None)
    assert d["kind"] == "watch"
    assert d["url"] == "https://live.douyin.com/123456789012"


def test_user_page_is_still_rejected():
    d = watchlink.decide("https://www.douyin.com/user/MS4wLjABAAAA", None)
    assert d["kind"] == "reject"
    assert "个人主页" in d["reason"]


def test_unresolved_short_link_is_kept_as_is():
    d = watchlink.decide("https://v.douyin.com/HHbdNjaE0jU",
                         lambda u: (u, "timed out"))
    assert d["url"] == "https://v.douyin.com/HHbdNjaE0jU"
    assert d["kind"] == "watch"


# ---------------------------------------------------------------- 分享文案里的主播名

# 抖音分享文案原文（飞书里粘过来的就是这个形状）。注意同一句话里还有
# 【抖音】，那不是主播名。
SHARE_TEXT = (
    "9- #在抖音，记录美好生活#【示例主播】正在直播，来和我一起支持Ta吧。"
    "复制下方链接，打开【抖音】，直接观看直播！ "
    "https://webcast.amemv.com/douyin/webcast/reflow/7700000000000000003")


def test_share_text_label_is_anchor_not_platform():
    d = watchlink.decide(SHARE_TEXT, None)
    assert d["kind"] == "watch"
    assert d["label"] == "示例主播"


def test_share_label_keeps_hyphens_in_anchor_name():
    # 名字里带连字符是常见的；不能按 "-" 切，只能认【】这个边界。
    got = watchlink.share_label("【示例ai落地派-企业AI赋能ai智能体】正在直播")
    assert got == "示例ai落地派-企业AI赋能ai智能体"


def test_share_label_returns_empty_for_platform_brackets():
    assert watchlink.share_label("来【抖音】看直播") == ""
    assert watchlink.share_label("打开【快手】就行") == ""
    assert watchlink.share_label("没有书名号的普通链接") == ""


def test_decide_label_is_empty_without_brackets():
    d = watchlink.decide("https://live.douyin.com/123456789012", None)
    assert d["label"] == ""
