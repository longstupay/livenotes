# -*- coding: utf-8 -*-
"""把 Python 引擎打成 onedir，放到 src-tauri/resources/engine/。

    python desktop/package-engine.py --with-models --with-ffmpeg

产物：
    desktop/src-tauri/resources/engine/engine.exe
    desktop/src-tauri/resources/engine/_internal/...
    desktop/src-tauri/resources/models/...   (可选)
    desktop/src-tauri/resources/ffmpeg/...   (可选)

注意：PyInstaller 的 onedir 启动比 onefile 快，也不容易被杀软盯上；
壳用 resources 路径 spawn，不走 externalBin。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parent
REPO = DESKTOP.parent
RESOURCES = DESKTOP / "src-tauri" / "resources"
ENGINE = RESOURCES / "engine"
BUILD = DESKTOP / ".build"


def run(cmd: list[str]) -> None:
    print("[package]", " ".join(str(c) for c in cmd[:8]), "...", flush=True)
    subprocess.run(cmd, cwd=REPO, check=True)


def copy_models() -> None:
    src = REPO / "models"
    dst = RESOURCES / "models"
    if not src.is_dir():
        raise SystemExit(f"没有模型目录：{src}（先 python tools/fetch_models.py）")
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src)
        target = dst / rel
        if target.is_file() and target.stat().st_size == f.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        print(f"[models] {rel} {f.stat().st_size/1e6:.1f}MB")


def copy_ffmpeg() -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("PATH 里没有 ffmpeg；装一个或去掉 --with-ffmpeg")
    src = Path(exe).resolve()
    dst = RESOURCES / "ffmpeg" / "ffmpeg.exe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[ffmpeg] {src} -> {dst} ({dst.stat().st_size/1e6:.1f}MB)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-models", action="store_true", help="把 models/ 复制进 resources/models")
    ap.add_argument("--with-ffmpeg", action="store_true", help="把 ffmpeg.exe 复制进 resources/ffmpeg")
    args = ap.parse_args()

    if ENGINE.exists():
        print(f"[clean] {ENGINE}")
        shutil.rmtree(ENGINE)
    BUILD.mkdir(parents=True, exist_ok=True)

    run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--name", "engine",
        "--distpath", str(RESOURCES),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        "--add-data", f"{REPO / 'app' / 'web'};app/web",
        # 桌面皮肤（overlay）：网页基座在 app/web，桌面专属的观感/文案/交互在
        # desktop/web。打包后落到 app/web-overlay，引擎启动时自动发现并优先出
        # 这一层的静态文件（见 app/webroot.py），不需要壳再传 --web-overlay。
        "--add-data", f"{REPO / 'desktop' / 'web'};app/web-overlay",
        # 桌面壳用 engine.exe --feishu-bot / --capture-agent 复用同一个入口，
        # 这两个脚本必须随 resources 走，否则打包态桌面端没有飞书和采集代理。
        "--add-data", f"{REPO / 'tools' / 'feishu_bot.py'};tools",
        "--add-data", f"{REPO / 'tools' / 'capture_agent.py'};tools",
        # direct 模式由 capture_agent 延迟导入，必须一起带走。
        "--add-data", f"{REPO / 'tools' / 'direct_capture.py'};tools",
        "--hidden-import", "websockets",
        "--hidden-import", "app.clips",
        "--hidden-import", "app.feishu",
        "--hidden-import", "app.profile",
        "--hidden-import", "app.report",
        "--hidden-import", "app.share",
        "--hidden-import", "app.timefmt",
        "--hidden-import", "app.watchlink",
        "--collect-all", "sherpa_onnx",
        "--collect-all", "lark_oapi",
        "--collect-all", "pycaw",
        "--collect-all", "comtypes",
        str(REPO / "run.py"),
    ])

    if not (ENGINE / "engine.exe").is_file():
        raise SystemExit(f"PyInstaller 没产出 {ENGINE / 'engine.exe'}")
    if args.with_models:
        copy_models()
    if args.with_ffmpeg:
        copy_ffmpeg()

    total = sum(f.stat().st_size for f in RESOURCES.rglob("*") if f.is_file())
    print(f"[done] resources = {total/1e6:.1f} MB")
    print(f"[done] engine exe = {ENGINE / 'engine.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
