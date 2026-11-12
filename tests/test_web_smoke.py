# -*- coding: utf-8 -*-
"""前端冒烟：页面能渲染、静态资源存在、JS 能解析。

重构（拆模块、抽公共文件）最怕的不是报错，是**静默白屏**：
某个 `<script src>` 少了个字母，页面照样 200，但什么都不显示。
这三条检查分别对应三种典型故障：

- ``test_pages_render``：模块 404 / 运行时报错 → 关键选择器不在 DOM 里；
- ``test_static_assets_exist``：HTML 引用了不存在的文件 → 静态检查就能抓住，比开浏览器快；
- ``test_js_files_parse``：语法错 → Node 解析一遍，不用等 CI 跑到浏览器。

没有 Chrome 的机器上第一条会 skip，后两条始终生效。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"

STATIC_REF = re.compile(r'(?:src|href)\s*=\s*["\'](/static/[^"\'?#]+)')


def test_pages_render():
    """十一个页面逐个用 headless Chrome 打开，断言关键节点存在、控制台无报错。"""
    res = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "ui_smoke.py"), "--json"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    if res.returncode == 3:
        pytest.skip("本机没有 Chrome，跳过浏览器冒烟")
    try:
        results = json.loads(res.stdout[res.stdout.index("["):])
    except (ValueError, json.JSONDecodeError):
        pytest.fail("冒烟脚本没有输出 JSON：\n%s\n%s" % (res.stdout, res.stderr))
    bad = [r for r in results if not r["ok"]]
    assert not bad, "以下页面冒烟失败：\n" + "\n".join(
        "%s: %s" % (r["page"], "; ".join(r["problems"])) for r in bad)


def test_static_assets_exist():
    """HTML 里引用的 /static/... 必须真的落盘（拆文件后最容易漏的一条）。"""
    missing = []
    for page in sorted(WEB.glob("*.html")):
        text = page.read_text(encoding="utf-8")
        for ref in set(STATIC_REF.findall(text)):
            target = WEB / ref[len("/static/"):]
            if not target.is_file():
                missing.append("%s -> %s" % (page.name, ref))
    assert not missing, "引用了不存在的静态资源：\n" + "\n".join(missing)


def test_js_files_parse(tmp_path):
    """app/web 下的 .js 用 Node 解析一遍。

    ESM 的 ``import`` 在 CommonJS 模式下会报错，所以复制成 .mjs 再检查；
    Node 不在的机器上跳过（CI 的 extension job 有 Node，pytest job 没有）。
    """
    node = shutil.which("node")
    files = sorted(WEB.rglob("*.js"))
    if not files:
        pytest.skip("还没有独立 JS 文件")
    if not node:
        pytest.skip("本机没有 Node，跳过 JS 语法检查")

    bad = []
    for js in files:
        # 不要写进 app/web：那目录是会被服务出去的
        tmp = tmp_path / (js.stem + ".check.mjs")
        tmp.write_bytes(js.read_bytes())
        res = subprocess.run([node, "--check", str(tmp)], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=30)
        if res.returncode != 0:
            bad.append("%s\n%s" % (js.relative_to(ROOT), res.stderr.strip()))
    assert not bad, "JS 语法错误：\n" + "\n\n".join(bad)
