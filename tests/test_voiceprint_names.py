# -*- coding: utf-8 -*-
"""角色词与簇号不能当人名写进人物库（前后端双重拦截的后端一侧）。"""
from __future__ import annotations

import pytest

from app import voiceprint


@pytest.mark.parametrize("name", [
    "", "  ", "主播", "嘉宾", "运营", "说话人", "未知", "路人", "观众",
    "网友", "连麦", "用户", "有人", "未识别", "嘉宾1", "说话人 2",
    "SPK_00", "spk-3",
])
def test_role_labels_are_not_person_names(name):
    assert voiceprint.is_generic_name(name)


@pytest.mark.parametrize("name", ["老王", "小A", "阿哲", "嘉宾老王", "运营总监周"])
def test_real_names_still_pass(name):
    assert not voiceprint.is_generic_name(name)
