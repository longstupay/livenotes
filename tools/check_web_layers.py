# -*- coding: utf-8 -*-
"""分层检查：``app/web``（基座）里不许出现桌面实现，``desktop/web``（overlay）
只许用锚点改观感。

对应 ``docs/29-page-host-contract.md`` 的三条硬规则：

1. 基座里除两个协议文件（``js/embed.js`` / ``css/embed.css``）外，不得出现
   ``__LN_DESKTOP__``、``ln-embed``、``embed=1``、``/static/css/paper.css``
   这类宿主专属标记——它们一出现，网页版就会替桌面背逻辑。
2. overlay 只能改观感：不放页面 HTML；CSS 选择器必须以 ``html`` /
   ``[data-ln-region]`` / ``[data-theme]`` / ``.ln-`` 开头，禁用 ``@import``
   （资源清单由 ``host.json`` 管）。
3. 契约对得上：``host.json`` 列的 css/js 必须真的在 overlay 里；``profile.copy``
   的 key 必须在基座 HTML 里被 ``data-ln-copy`` 用到（防过期文案）。

用法::

    python tools/check_web_layers.py            # 仓库默认路径
    python tools/check_web_layers.py --overlay  # 只查 overlay（桌面 PR 用）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"
OVERLAY = ROOT / "desktop" / "web"

#: 允许出现宿主标记的两个协议文件（相对 app/web）
PROTOCOL_FILES = {"js/embed.js", "css/embed.css"}

#: 基座里出现即失败的字面量
FORBIDDEN_BASE = (
    ("__LN_DESKTOP__", "壳注入的全局标记；改用 LN.host.has(...) 能力查询"),
    ("ln-embed", "嵌入标记类；属于宿主适配器（js/embed.js）"),
    ("embed=1", "嵌入参数；属于宿主适配器（js/embed.js）"),
    ("/static/css/paper.css", "桌面皮肤；应该在 desktop/web/css/"),
)

#: overlay CSS 选择器允许的起点
SELECTOR_OK = re.compile(r"^(html(?![a-z])|\[data-ln-region|\[data-theme|\.ln-)")
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
CSS_RULE = re.compile(r"([^{}]+)\{")

COPY_KEY = re.compile(r'data-ln-copy="([^"]+)"')
EMBEDDED_PAGE = re.compile(r'<script[^>]+src="/static/js/embed\.js"')

_scan_suffixes = {".html", ".css", ".js"}


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(p)


def _iter_files(root: Path, suffixes: set[str]):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in suffixes:
            yield p


def check_base(problems: list[str]) -> None:
    for path in _iter_files(WEB, _scan_suffixes):
        rel = str(path.relative_to(WEB)).replace("\\", "/")
        if rel in PROTOCOL_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for token, why in FORBIDDEN_BASE:
            if token in text:
                problems.append("%s：出现 %s（%s）" % (_rel(path), token, why))

    # 每个可嵌入页面都要有 topbar / stage 锚点，否则皮肤只能回去写结构选择器。
    for page in sorted(WEB.glob("*.html")):
        text = page.read_text(encoding="utf-8", errors="replace")
        if not EMBEDDED_PAGE.search(text):
            continue
        for region in ("topbar", "stage"):
            if 'data-ln-region="%s"' % region not in text:
                problems.append("%s：缺少 data-ln-region=\"%s\" 锚点" % (_rel(page), region))


def _check_selector(sel: str, where: str, problems: list[str]) -> None:
    first = sel.strip().split(",")[0].strip()
    if not first or first.startswith("@"):
        problems.append("%s：不支持 %r（媒体查询/导入请进基座）" % (where, first[:40]))
        return
    if not SELECTOR_OK.match(first):
        problems.append(
            "%s：选择器 %r 没有从锚点起头；只允许 html / [data-ln-region] / "
            "[data-theme] / .ln- 开头" % (where, first[:60]))


def check_overlay(problems: list[str]) -> dict:
    if not OVERLAY.is_dir():
        problems.append("找不到 overlay 目录：%s" % _rel(OVERLAY))
        return {}
    for page in OVERLAY.glob("*.html"):
        problems.append("%s：overlay 不放页面 HTML（那是分叉，不是皮肤）" % _rel(page))

    for css in _iter_files(OVERLAY, {".css"}):
        text = CSS_COMMENT.sub("", css.read_text(encoding="utf-8", errors="replace"))
        if "@import" in text:
            problems.append("%s：用 host.json 的 css 清单加载，不要在 CSS 里 @import"
                            % _rel(css))
        for raw in CSS_RULE.findall(text):
            for sel in raw.split(","):
                if sel.strip():
                    _check_selector(sel, _rel(css), problems)

    for js in _iter_files(OVERLAY, {".js"}):
        text = js.read_text(encoding="utf-8", errors="replace")
        if re.search(r"[A-Za-z]:\\\\|file://", text):
            problems.append("%s：出现本机绝对路径" % _rel(js))

    manifest_path = OVERLAY / "host.json"
    if not manifest_path.is_file():
        problems.append("%s：缺少 host.json" % _rel(OVERLAY))
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        problems.append("%s：JSON 解析失败：%s" % (_rel(manifest_path), e))
        return {}
    for kind in ("css", "js"):
        for item in manifest.get(kind) or []:
            s = str(item)
            if not s.startswith("/static/"):
                problems.append("%s：%s 必须以 /static/ 开头：%s"
                                % (_rel(manifest_path), kind, s))
            elif not (OVERLAY / s[len("/static/"):]).is_file():
                problems.append("%s：%s 指向不存在的文件：%s"
                                % (_rel(manifest_path), kind, s))
    return manifest


def check_contract(manifest: dict, problems: list[str]) -> None:
    copy = ((manifest.get("profile") or {}).get("copy") or {})
    if not copy:
        return
    used = set()
    for page in WEB.glob("*.html"):
        used.update(COPY_KEY.findall(page.read_text(encoding="utf-8", errors="replace")))
    for key in copy:
        if key not in used:
            problems.append("host.json：copy.%s 在基座里没有对应的 data-ln-copy，"
                            "文案已经过期" % key)


def main() -> int:
    ap = argparse.ArgumentParser(description="检查基座与 overlay 的分层")
    ap.add_argument("--overlay", action="store_true", help="只查 overlay")
    args = ap.parse_args()

    problems: list[str] = []
    manifest: dict = {}
    if not args.overlay:
        check_base(problems)
    manifest = check_overlay(problems)
    check_contract(manifest, problems)

    if problems:
        print("分层检查失败（%d 处）：" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1
    print("分层检查通过：基座无宿主标记，overlay 只用锚点（契约见 docs/29）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
