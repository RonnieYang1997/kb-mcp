# -*- coding: utf-8 -*-
"""模型取舍质量对比：fp32 与 int8 量化的向量一致性与检索一致性（只读取样）。

判据：
  mean_cos  —— 同一文本在两种模型下向量的平均余弦（越接近 1 越好）
  top1/top5 —— 用标题当查询，在同一样本内检索，两种模型排序的 Top-1 / Top-5 重合率
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sample(n_chunks: int, n_queries: int):
    from kb import textproc
    repo = Path(r"C:\Users\Ronnie\Documents\GitHub\dufuzhixin")
    files = sorted(repo.glob("**/dufu-BV*.md"))
    texts, queries, titles = [], [], []
    step = max(1, len(files) // 200)
    for i in range(0, len(files), step):
        if len(texts) >= 200:
            break
        with open(files[i], "rb") as f:
            raw = f.read(400_000)
        t = raw.decode("utf-8", errors="replace")
        meta, body, _ = textproc.parse_front_matter(t)
        m = textproc.extract_meta(meta, body, files[i].name)
        clean, _ = textproc.clean_body(body, m["title"], True)
        if textproc.detect_dead(clean, meta) or len(clean) < 400:
            continue
        chunks = textproc.chunk_text(clean, 350, 80)
        if not chunks:
            continue
        texts.extend(chunks[:3])
        if len(titles) < n_queries and len(m["title"]) >= 4:
            titles.append(m["title"])
            queries.append(chunks[0][:200])
    return texts[:n_chunks], queries[:n_queries], titles[:n_queries]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", type=int, default=120)
    ap.add_argument("--queries", type=int, default=12)
    args = ap.parse_args()
    from kb import config as cfgmod
    from kb.embed import Embedder
    cfg = cfgmod.load()
    md = cfg["embed"]["model_dir"]
    texts, queries, titles = sample(args.chunks, args.queries)
    print(f"[compare] 样本 {len(texts)} 块, 查询 {len(queries)} 条")

    a = Embedder(md, model_file="onnx/model.onnx").load()
    b = Embedder(md, model_file="onnx/model_quantized.onnx").load()
    print(f"[compare] fp32 available={a.available} int8 available={b.available}")
    if not (a.available and b.available):
        return 1
    va = a.encode(texts)
    vb = b.encode(texts)
    cos = np.sum(va * vb, axis=1)
    print(f"[compare] 向量一致性: mean_cos={cos.mean():.5f} min={cos.min():.5f} "
          f"p1={np.percentile(cos, 1):.5f}")

    qa = a.encode(queries, prefix=cfg["embed"]["query_prefix"])
    qb = b.encode(queries, prefix=cfg["embed"]["query_prefix"])
    for name, q in (("fp32", qa), ("int8", qb)):
        sims = q @ (va if name == "fp32" else vb).T
        top5 = np.argsort(-sims, axis=1)[:, :5]
        if name == "fp32":
            ref5 = top5
        else:
            t1 = (top5[:, 0] == ref5[:, 0]).mean()
            ov = np.mean([len(set(top5[i]) & set(ref5[i])) / 5 for i in range(len(top5))])
            print(f"[compare] 检索一致性: Top-1 重合 {t1 * 100:.1f}%  Top-5 平均重合 {ov * 100:.1f}%")
    print("[compare] 查询示例:")
    for t, q in list(zip(titles, queries))[:5]:
        print(f"    {t[:40]}  |  {q[:40]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())