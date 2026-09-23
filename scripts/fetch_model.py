# -*- coding: utf-8 -*-
"""下载 bge-small-zh-v1.5 的 ONNX 模型与分词器（约 100MB）。

huggingface.co 在本机不可达 → 默认走 hf-mirror.com（已验证可达）；也可用 --proxy。
下载完成后做一次真实加载校验，确保第二步不会卡在模型上。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "models" / "bge-small-zh-v1.5"
HF_BASES = ("https://hf-mirror.com", "https://huggingface.co")
MS_BASE = "https://modelscope.cn"
# BAAI 官方仓库没有 onnx/ 目录，ONNX 权重取 Xenova 的转换版本（同一套权重与分词器）
REPOS = ("Xenova/bge-small-zh-v1.5", "BAAI/bge-small-zh-v1.5")
FILES = ["onnx/model.onnx", "tokenizer.json"]


def urls_for(f: str) -> list[str]:
    out = [f"{b}/{repo}/resolve/main/{f}" for b in HF_BASES for repo in REPOS]
    out += [f"{MS_BASE}/models/{repo}/resolve/master/{f}" for repo in REPOS]
    return out


def fetch(url: str, dest: Path, proxy: str = "") -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "kb-mcp-fetch/0.1"})
    t0 = time.time()
    try:
        with opener.open(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            last = 0
            while True:
                buf = r.read(262144)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if done - last > 5_000_000 or (total and done == total):
                    last = done
                    pct = f"{done * 100 // total}%" if total else f"{done // 1024 // 1024}MB"
                    print(f"    {dest.name}: {done // 1024 // 1024}MB / "
                          f"{total // 1024 // 1024 if total else '?'}MB ({pct})", flush=True)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"    失败: {type(e).__name__}: {e}", flush=True)
        return False
    tmp.replace(dest)
    print(f"    完成 {dest}  {dest.stat().st_size // 1024 // 1024}MB  {time.time() - t0:.0f}s", flush=True)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--proxy", default=os.environ.get("KB_HTTP_PROXY", ""))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--file", action="append", help="额外/指定的文件（相对仓库路径），可重复")
    args = ap.parse_args()
    files = args.file or FILES
    out = Path(args.dir)
    print(f"[fetch_model] 目标目录 {out}")
    ok = True
    for f in files:
        dest = out / f
        if dest.exists() and not args.force and dest.stat().st_size > 1024:
            print(f"  已存在，跳过 {dest} ({dest.stat().st_size // 1024 // 1024}MB)")
            continue
        urls = urls_for(f)
        if args.proxy:
            urls = list(reversed(urls))
        got = False
        for u in urls:
            print(f"  下载 {u}")
            if fetch(u, dest, args.proxy):
                got = True
                break
        if not got:
            print(f"  ✗ 无法获取 {f}", file=sys.stderr)
            ok = False
    if not ok:
        return 1
    # 加载校验
    sys.path.insert(0, str(ROOT))
    from kb.embed import Embedder
    e = Embedder(str(out)).load()
    print(f"[fetch_model] 加载校验: available={e.available} reason={e.reason}")
    if e.available:
        import numpy as np
        v = e.encode(["独夫之心 语音转录 测试"], prefix="")
        print(f"[fetch_model] 向量维度={v.shape} 归一化={float(np.linalg.norm(v[0])):.4f}")
    return 0 if e.available else 1


if __name__ == "__main__":
    raise SystemExit(main())