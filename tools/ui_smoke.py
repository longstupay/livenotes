#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端冒烟：起一个临时服务，用 headless Chrome 逐页打开，抓白屏与 JS 报错。

为什么需要它：`index.html` 有 122 个顶层函数、0 个前端测试。重构（拆模块、抽文件）
最典型的故障就是某个模块 404 或语法错导致整页白屏——靠人点十来个页面很容易漏，
而且漏掉的那次往往是在改完 rooms 的第二天。

这个脚本只看三件事：

1. 页面能打开（HTTP 200）；
2. 关键选择器在 DOM 里（说明脚本跑到了渲染那一步，没有半路抛异常）；
3. 控制台没有 Uncaught / SyntaxError / 资源 404。

用法::

    python tools/ui_smoke.py                     # 自动找 Chrome
    python tools/ui_smoke.py --chrome <path>     # 指定浏览器
    python tools/ui_smoke.py --only index rooms # 只跑部分页面
    python tools/ui_smoke.py --shots out/       # 顺便存截图，方便肉眼比对

退出码：0 全过；1 有页面失败；3 找不到 Chrome（CI 里据此跳过）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 每页至少要有这些元素，才算「脚本跑起来了」而不是白屏。
# 只挑首屏稳定存在的结构节点，不挑会被数据改写的文本。
# 统一挂一场演示数据：空库时 reader 会整页换成「缺少 session 参数」，
# 那样冒烟只能证明"错误页没崩"，证明不了正文渲染链路。
DEMO = "DEMO_20261104_2000"
PAGES = [
    ("index", "/?session=" + DEMO, ["#sessTitle", "#sumBody", ".grid"]),
    ("rooms", "/rooms", ["#wall", "#urlInput", "#addBtn"]),
    ("live", "/live?session=" + DEMO, ["#sum", "#list"]),
    ("reader", "/reader?session=" + DEMO, ["#title", "#meta", "#stats"]),
    ("library", "/library", [".cards", "#tabs", "#q"]),
    ("people", "/people", ["#cards", "#plist", "#q"]),
    ("highlights", "/highlights?session=" + DEMO, ["#list", "#fbStat", "#title"]),
    ("capture", "/capture", ["#state", "#dot", "#err"]),
    ("pushes", "/pushes", ["#rows", "#chips"]),
    ("glossary", "/glossary", ["#rows", "#add", "#save"]),
    ("metrics", "/metrics", ["#kpis", "#sessions", "#events"]),
]

# 控制台里算“失败”的行。Chrome 把页面 JS 的报错写成 [ERROR:CONSOLE(n)] "..."，
# 资源加载失败写成 Failed to load resource。
BAD_MARKERS = (
    "Uncaught",
    "SyntaxError",
    "ReferenceError",
    "TypeError",
    "Failed to load resource",
    "net::ERR",
)

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def find_chrome(explicit: str | None = None) -> str | None:
    for c in [explicit or ""] + CHROME_CANDIDATES:
        if c and Path(c).is_file():
            return c
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser"):
        p = shutil.which(name)
        if p:
            return p
    return None


def start_server(tmp: Path):
    """起一个用临时数据库的服务，端口交给系统分配，不碰真实 data/。"""
    from app import live, server, store

    # 请求日志会把真正的失败信息埋掉：这里只关心浏览器端的报错
    server.Handler.log_message = lambda *a, **k: None
    tmp.mkdir(parents=True, exist_ok=True)
    store.DB_PATH = tmp / "smoke.db"
    store.ensure_dirs = lambda: None
    live.DATA_DIR = tmp
    live.AUDIO_DIR = tmp / "audio"
    live.FINISH_LOG = tmp / "finalize.log"
    (tmp / "audio").mkdir(exist_ok=True)
    conn = store.connect()
    store.init_db(conn)
    from contextlib import redirect_stdout
    from io import StringIO

    from app import seed_demo
    with redirect_stdout(StringIO()):  # seed 会打印行数，别污染 --json 输出
        seed_demo.seed(conn)
    conn.close()

    srv = server.make_server("127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def run_chrome(chrome: str, url: str, profile: Path, budget: int,
               shot: Path | None, size: str = "1440,900") -> tuple[str, str]:
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--user-data-dir=%s" % Path(profile).resolve(),
        "--window-size=%s" % size,
        "--virtual-time-budget=%d" % budget,
        "--enable-logging=stderr",
        "--log-level=0",
        "--dump-dom",
    ]
    if os.environ.get("CI") or os.environ.get("CHROME_NO_SANDBOX"):
        cmd.append("--no-sandbox")  # 容器里的 runner 常因 user namespace 起不来
    cmd.append(url)
    try:
        res = subprocess.run(cmd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=45)
        dom, err = res.stdout or "", res.stderr or ""
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        err = (e.stderr or b"")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return out, err + "\n[TIMEOUT] chrome 超过 45s 没有退出"

    if shot:
        # --dump-dom 与 --screenshot 同时给会二选一（实测截图被吞），分开跑
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot_cmd = [c for c in cmd[:-1] if c != "--dump-dom" and not c.startswith("--screenshot=")]
        shot_cmd += ["--screenshot=%s" % Path(shot).resolve(), url]
        try:
            subprocess.run(shot_cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=45)
        except subprocess.TimeoutExpired:
            pass
    return dom, err


def check_page(name: str, base: str, chrome: str, profile: Path, budget: int,
               shotdir: Path | None) -> dict:
    url = base + dict((n, p) for n, p, _ in PAGES)[name]
    required = dict((n, r) for n, _, r in PAGES)[name]
    shot = (shotdir / ("%s.png" % name)) if shotdir else None
    dom, err = run_chrome(chrome, url, profile, budget, shot)

    problems = []
    if not dom.strip():
        problems.append("DOM 为空（页面没渲染）")
    for sel in required:
        # 只认 id / class 两种，够用了，不引入解析器
        if sel.startswith("#") and ('id="%s"' % sel[1:]) not in dom:
            problems.append("缺少 %s" % sel)
        elif sel.startswith(".") and ('class="%s"' % sel[1:]) not in dom \
                and (' %s' % sel[1:]) not in dom:
            problems.append("缺少 %s" % sel)
    for line in err.splitlines():
        if any(m in line for m in BAD_MARKERS):
            problems.append(line.strip()[:300])
    return {"page": name, "url": url, "ok": not problems, "problems": problems,
            "dom_len": len(dom)}


def main() -> int:
    # Windows 控制台默认 GBK，中文提示会变成乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", default=None)
    ap.add_argument("--only", nargs="*", default=None,
                    help="只跑这些页面：%s" % " ".join(n for n, _, _ in PAGES))
    ap.add_argument("--budget", type=int, default=4000, help="virtual-time-budget(ms)")
    ap.add_argument("--shots", default=None, help="截图输出目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 CI 用）")
    args = ap.parse_args()

    chrome = find_chrome(args.chrome)
    if not chrome:
        print("找不到 Chrome。用 --chrome 指定，或设 CHROME_PATH。", file=sys.stderr)
        return 3

    pages = [p for p in PAGES if not args.only or p[0] in args.only]
    # --json 是给 CI 解析的：服务端起来之后 logs.log_event 会往 stdout 打人读日志，
    # 混在 JSON 前面就成了非法 JSON（test_web_smoke 就是这么挂的）。跑页面期间把
    # stdout 挪到 stderr，最后再往真正的 stdout 输出一次结果。
    out = sys.stdout
    quiet = (contextlib.redirect_stdout(sys.stderr) if args.json
             else contextlib.nullcontext())
    with quiet:
        with tempfile.TemporaryDirectory(prefix="lnsmoke-") as td:
            tmp = Path(td)
            srv, port = start_server(tmp / "data")
            base = "http://127.0.0.1:%d" % port
            try:
                # 服务起来第一下先探活，避免把「服务没起」误报成「页面白屏」
                for _ in range(50):
                    try:
                        urlopen(base + "/api/health", timeout=1).read()
                        break
                    except Exception:
                        time.sleep(0.1)
                results = []
                for name, _, _ in pages:
                    results.append(check_page(name, base, chrome, tmp / ("prof-" + name),
                                              args.budget,
                                              Path(args.shots) if args.shots else None))
            finally:
                srv.shutdown()
                srv.server_close()

        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2), file=out)
        else:
            for r in results:
                mark = "PASS" if r["ok"] else "FAIL"
                print("%-6s %-12s dom=%6d  %s" % (mark, r["page"], r["dom_len"],
                                                  r["url"]), file=out)
                for p in r["problems"]:
                    print("        - " + p, file=out)
            bad = [r for r in results if not r["ok"]]
            print("\n%d/%d 通过" % (len(results) - len(bad), len(results)), file=out)
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
