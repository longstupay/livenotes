# -*- coding: utf-8 -*-
"""下载实时监听所需的三个本地模型（约 270MB）。

    python tools/fetch_models.py

默认走国内可访问的镜像（hf-mirror / gh-proxy），失败自动回退官方地址。
已存在的文件会跳过；加 --force 可重新下载。
"""
from __future__ import annotations

import concurrent.futures as cf
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

FILES = {
    "sensevoice/model.int8.onnx": (
        239_233_841,
        ["https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx",
         "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/model.int8.onnx"],
    ),
    "sensevoice/tokens.txt": (
        315_894,
        ["https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt",
         "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/tokens.txt"],
    ),
    "vad/silero_vad.onnx": (
        643_854,
        ["https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx",
         "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"],
    ),
    "speaker/campplus.onnx": (
        28_281_138,
        ["https://gh-proxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
         "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
         "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
         "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"],
    ),
}


def _get(url: str, start: int | None = None, end: int | None = None, timeout: int = 90) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (LiveNotes model fetcher)"}
    if start is not None:
        headers["Range"] = f"bytes={start}-{end}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parallel(url: str, dest: Path, size: int, n: int = 6) -> bool:
    step = (size + n - 1) // n
    def part(i: int):
        a = i * step
        b = min(size, a + step) - 1
        if a >= size:
            return b""
        last = None
        for attempt in range(4):
            try:
                data = _get(url, a, b)
                if len(data) == b - a + 1:
                    return data
                last = f"short {len(data)}/{b - a + 1}"
            except Exception as e:
                last = str(e)
            time.sleep(1.2 * (attempt + 1))
        raise RuntimeError(f"part {i} failed: {last}")
    try:
        with cf.ThreadPoolExecutor(max_workers=n) as ex:
            chunks = list(ex.map(part, range(n)))
    except Exception as e:
        print(f"    并行下载失败（{e}），改用单连接")
        return False
    tmp = dest.with_suffix(dest.suffix + ".part")
    with open(tmp, "wb") as f:
        for d in chunks:
            f.write(d)
    os.replace(tmp, dest)
    return True


def download(rel: str, size: int, urls: list[str], force: bool = False) -> None:
    dest = MODELS / rel
    if dest.is_file() and dest.stat().st_size == size and not force:
        print(f"[skip] {rel} ({size/1e6:.1f}MB)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    for url in urls:
        print(f"[get ] {rel} <- {url.split('/')[2]}")
        try:
            if size > 8_000_000 and _parallel(url, dest, size):
                pass
            else:
                tmp = dest.with_suffix(dest.suffix + ".part")
                with urllib.request.urlopen(
                        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as r, \
                        open(tmp, "wb") as f:
                    while True:
                        block = r.read(1 << 20)
                        if not block:
                            break
                        f.write(block)
                os.replace(tmp, dest)
            got = dest.stat().st_size
            if got == size:
                print(f"       ok {got/1e6:.1f}MB")
                return
            print(f"       size mismatch {got}/{size}, retry")
            dest.unlink(missing_ok=True)
        except Exception as e:
            print(f"       failed: {e}")
    raise SystemExit(f"下载失败：{rel}（可手动放到 {dest}）")


def main() -> int:
    force = "--force" in sys.argv
    for rel, (size, urls) in FILES.items():
        download(rel, size, urls, force)
    print("\n模型已就绪：", MODELS)
    print("启动：python run.py  ->  页面右上角「● 实时监听」")
    return 0


if __name__ == "__main__":
    sys.exit(main())
