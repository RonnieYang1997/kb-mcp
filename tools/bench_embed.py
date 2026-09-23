# -*- coding: utf-8 -*-
"""向量化性能基准：决定全量索引的真实耗时与线程/模型配置。

用法：python tools/bench_embed.py [--chunks 64] [--threads 4,8] [--model fp32|int8]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sample_chunks(n: int) -> list[str]:
    """从真实语料只读取样切块（不写任何文件）。"""
    from kb import textproc
    repo = Path(r"C:\Users\Ronnie\Documents\GitHub\dufuzhixin")
    files = sorted(repo.glob("**/dufu-BV*.md"))
    add = 0
    seen: set[int] = set()
    chunks: list[str] = []
    texts = []
    while len(chunks) < n and add < len(files):
        i = (add * 37) % len(files)
        add += 1
        if i in seen:
            continue
        seen.add(i)
        with open(files[i], "rb") as f:
            raw = f.read(400_000)
        text = raw.decode("utf-8", errors="replace")
        _meta, body, _ = textproc.parse_front_matter(text)
        texts.append(body)
    for body in texts:
        chunks += textproc.chunk_text(body, 350, 80)
        if len(chunks) >= n:
            break
    return chunks[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=64)
    ap.add_argument("--threads", default="")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--model", default="", help="模型文件相对路径，默认用 config 里的")
    args = ap.parse_args()

    from kb import config as cfgmod
    cfg = cfgmod.load()
    model_dir = Path(cfg["embed"]["model_dir"])
    print(f"[bench] python={sys.version.split()[0]} model_dir={model_dir}")
    print(f"[bench] onnxruntime={__import__('onnxruntime').__version__} "
          f"cpu_count={os.cpu_count()}")

    chunks = sample_chunks(args.chunks)
    chars = sum(len(c) for c in chunks)
    print(f"[bench] 样本 {len(chunks)} 块, 共 {chars} 字, 平均 {chars // max(1, len(chunks))} 字")

    thread_opts = [int(x) for x in args.threads.split(",")] if args.threads else [0]
    results = []
    for th in thread_opts:
        if th:
            os.environ["OMP_NUM_THREADS"] = str(th)
        import onnxruntime as ort
        from kb.embed import Embedder
        e = Embedder(str(model_dir), 512, 512, args.batch)
        e.load()
        if not e.available:
            print("[bench] 加载失败: " + e.reason)
            return 1
        if th or args.model:
            import onnxruntime as ort2
            so = ort2.SessionOptions()
            if th:
                so.intra_op_num_threads = th
                so.inter_op_num_threads = 1
            path = (model_dir / args.model) if args.model else (model_dir / "onnx" / "model.onnx")
            e._sess = ort2.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
            e._inputs = [i.name for i in e._sess.get_inputs()]
            print(f"[bench] session 文件={path.name} size={path.stat().st_size // 1024 // 1024}MB")
        t0 = time.time()
        _ = e.encode(["预热"], prefix="")
        warm = time.time() - t0
        t0 = time.time()
        v = e.encode(chunks, prefix="")
        dt = time.time() - t0
        row = {"threads": th or "default", "batch": args.batch, "chunks": len(chunks),
               "seconds": round(dt, 2), "per_chunk_ms": round(dt * 1000 / len(chunks), 1),
               "chunks_per_hour": int(len(chunks) / dt * 3600), "warm_s": round(warm, 2),
               "dim": int(v.shape[1])}
        results.append(row)
        print("[bench] " + json.dumps(row, ensure_ascii=False))
    if results:
        best = max(results, key=lambda r: r["chunks_per_hour"])
        print(f"\n[bench] 最快: threads={best['threads']} {best['chunks_per_hour']} chunks/h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())