# -*- coding: utf-8 -*-
"""全库规模与耗时预估（只读：只做解码/清洗/切块计数，不写任何文件、不建索引）。

用法：python tools/estimate.py [--rate 5000]   # rate = 每小时可向量化的片段数
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=0.0, help="实测向量化速度：片段/小时")
    args = ap.parse_args()

    from kb import config as cfgmod, indexer, textproc
    cfg = cfgmod.load()
    csize = cfg["chunk"]["size"]
    cover = cfg["chunk"]["overlap"]
    print(f"[estimate] 切块 size={csize} overlap={cover} 源={[s['root'] for s in cfg['sources']]}")
    t0 = time.time()
    grand = {"files": 0, "chars_clean": 0, "chunks": 0, "dead": 0, "empty": 0, "max_chars": 0}
    for src in cfg["sources"]:
        files = indexer.iter_files(src)
        st = {"files": 0, "chars_clean": 0, "chunks": 0, "dead": 0, "empty": 0, "max_chars": 0}
        for f in files:
            with open(f["abs"], "rb") as fh:
                raw = fh.read(int(cfg["scan"]["max_file_bytes"]))
            text = raw.decode("utf-8", errors="replace")
            meta, body, _ = textproc.parse_front_matter(text)
            m = textproc.extract_meta(meta, body, f["name"])
            clean, _s = textproc.clean_body(body, m["title"], bool(src.get("clean", True)))
            st["files"] += 1
            st["max_chars"] = max(st["max_chars"], len(clean))
            if textproc.detect_dead(clean, meta):
                st["dead"] += 1
                continue
            if len(clean) < cfg["scan"]["min_body_chars"]:
                st["empty"] += 1
                continue
            st["chars_clean"] += len(clean)
            st["chunks"] += len(textproc.chunk_text(clean, csize, cover))
        print(f"[estimate] {src['id']}: " + ", ".join(f"{k}={v}" for k, v in st.items()))
        for k in grand:
            grand[k] = max(grand[k], st[k]) if k == "max_chars" else grand[k] + st[k]
    print(f"[estimate] 合计: " + ", ".join(f"{k}={v}" for k, v in grand.items()))
    print(f"[estimate] 扫描耗时 {time.time() - t0:.1f}s（单进程，仅解码+清洗+切块，未向量化）")
    if args.rate:
        hours = grand["chunks"] / args.rate
        print(f"[estimate] 按 {args.rate:.0f} 片段/小时 → 向量化预计 {hours:.1f} 小时 "
              f"（{hours * 60:.0f} 分钟）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())