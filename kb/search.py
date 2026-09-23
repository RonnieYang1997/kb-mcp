# -*- coding: utf-8 -*-
"""混合检索：FTS5(trigram) 全文 + 向量余弦，用 RRF(k=60) 融合。

设计要点（对应 KMCP 的失败点）：
- 中文不再走「一文档一 chunk」：候选来自 350 字块级索引。
- 纯向量会漂、纯全文会漏 → RRF 免调参融合。
- 查询词构造：中文整串优先按 3-gram 拆成 OR 项，长句也能命中（不是短语匹配）。
- 短查询（<3 字符）trigram 无法命中 → 自动降级 LIKE 扫描。
"""
from __future__ import annotations

import re
import time

import numpy as np

from . import store, textproc

CJK = r"\u3400-\u9fff\uf900-\ufaff"
RE_TOKENS = re.compile(fr"[{CJK}]+|[A-Za-z0-9_]+")


def build_match_expr(query: str, max_terms: int = 16) -> str:
    """把自然语言查询拆成 OR 连接的引号短语（trigram 索引下等价于 3-gram 命中）。"""
    terms: list[str] = []
    for tok in RE_TOKENS.findall(query or ""):
        if re.fullmatch(f"[{CJK}]+", tok):
            if len(tok) <= 4:
                terms.append(tok)
            else:
                for i in range(0, len(tok) - 2):
                    terms.append(tok[i:i + 3])
        else:
            if len(tok) >= 3:
                terms.append(tok)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= max_terms:
            break
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in out)


def _doc_filter(source: str | None, date_from: str | None, date_to: str | None,
                include_dead: bool = False) -> tuple[str, list]:
    where = [] if include_dead else ["d.state='indexed'"]
    params: list = []
    if source:
        where.append("d.source_id=?")
        params.append(source)
    if date_from:
        where.append("d.date>=?")
        params.append(date_from)
    if date_to:
        where.append("d.date<=?")
        params.append(date_to)
    return (" AND ".join(where) if where else "1=1"), params


def fts_search(conn, query: str, limit: int, where: str, params: list,
               max_terms: int = 16) -> tuple[list[tuple[int, float]], str]:
    expr = build_match_expr(query, max_terms)
    notes = []
    rows: list[tuple[int, float]] = []
    if expr:
        sql = (f"SELECT f.rowid AS cid, bm25(chunks_fts) AS s FROM chunks_fts f "
               f"JOIN chunks c ON c.id=f.rowid JOIN docs d ON d.id=c.doc_id "
               f"WHERE chunks_fts MATCH ? AND {where} ORDER BY s LIMIT ?")
        try:
            rows = [(r["cid"], float(r["s"])) for r in conn.execute(sql, [expr] + params + [limit])]
        except Exception as e:
            notes.append(f"FTS 查询失败，已降级: {type(e).__name__}")
            rows = []
    if not rows:
        q = (query or "").strip()
        if len(q) >= 2:
            sql = (f"SELECT c.id AS cid FROM chunks c JOIN docs d ON d.id=c.doc_id "
                   f"WHERE {where} AND c.text LIKE ? LIMIT ?")
            rows = [(r["cid"], 0.0) for r in conn.execute(sql, params + [f"%{q}%", limit])]
            if rows:
                notes.append("trigram 未命中，已降级为子串扫描")
    return rows, ("; ".join(notes))


def vector_search(conn, qvec: np.ndarray, limit: int, where: str, params: list
                  ) -> list[tuple[int, float]]:
    ids, mat = store.load_vectors(conn, where, tuple(params))
    if not ids or mat.size == 0:
        return []
    sims = mat @ qvec.reshape(-1).astype(np.float32)
    k = min(limit, len(ids))
    idx = np.argpartition(-sims, k - 1)[:k] if k < len(ids) else np.arange(len(ids))
    idx = idx[np.argsort(-sims[idx])]
    return [(ids[int(i)], float(sims[int(i)])) for i in idx]


def rrf_fuse(rank_lists: list[list[tuple[int, float]]], k: int = 60) -> dict[int, float]:
    fused: dict[int, float] = {}
    for lst in rank_lists:
        for rank, (cid, _score) in enumerate(lst, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    return fused


def search(conn, cfg: dict, query: str, embedder=None, top_k: int | None = None,
           source: str | None = None, mode: str = "hybrid",
           date_from: str | None = None, date_to: str | None = None) -> dict:
    scfg = cfg.get("search", {})
    top_k = int(top_k or scfg.get("default_top_k", 8))
    top_k = max(1, min(top_k, int(scfg.get("max_top_k", 50))))
    fts_lim = int(scfg.get("fts_candidates", 200))
    vec_lim = int(scfg.get("vector_candidates", 200))
    k = int(scfg.get("rrf_k", 60))
    t0 = time.time()
    notes: list[str] = []
    where, params = _doc_filter(source, date_from, date_to)

    fts_rows: list[tuple[int, float]] = []
    vec_rows: list[tuple[int, float]] = []
    if mode in ("hybrid", "fts"):
        fts_rows, note = fts_search(conn, query, fts_lim, where, params, scfg.get("max_terms", 16))
        if note:
            notes.append(note)
    if mode in ("hybrid", "vector"):
        if embedder is not None and getattr(embedder, "available", False):
            prefix = cfg.get("embed", {}).get("query_prefix", "")
            qvec = embedder.encode([query], prefix=prefix)[0]
            vec_rows = vector_search(conn, qvec, vec_lim, where, params)
        else:
            notes.append("向量检索不可用（" + (getattr(embedder, "reason", "未加载") if embedder else "未加载") + "）")

    if mode == "vector":
        fused = {cid: 1.0 / (k + i) for i, (cid, _) in enumerate(vec_rows, 1)}
    elif mode == "fts":
        fused = {cid: 1.0 / (k + i) for i, (cid, _) in enumerate(fts_rows, 1)}
    else:
        fused = rrf_fuse([fts_rows, vec_rows], k)

    fts_rank = {cid: i for i, (cid, _) in enumerate(fts_rows, 1)}
    vec_rank = {cid: i for i, (cid, _) in enumerate(vec_rows, 1)}
    vec_sim = {cid: s for cid, s in vec_rows}
    ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
    meta = store.doc_of_chunk(conn, [cid for cid, _ in ordered])

    snip_len = int(scfg.get("snippet_chars", 240))
    results = []
    for rank, (cid, score) in enumerate(ordered, start=1):
        m = meta.get(cid)
        if not m:
            continue
        results.append({
            "rank": rank,
            "score": round(score, 6),
            "doc_id": m["doc_id"],
            "chunk_id": cid,
            "seq": m["seq"],
            "title": m["title"],
            "bvid": m["bvid"],
            "date": m["date"],
            "source": m["source_id"],
            "path": m["rel_path"],
            "chars": m["n_chars"],
            "fts_rank": fts_rank.get(cid),
            "vector_rank": vec_rank.get(cid),
            "cosine": round(vec_sim[cid], 4) if cid in vec_sim else None,
            "snippet": textproc.snippet(m["text"], snip_len),
        })
    return {
        "query": query,
        "mode": mode,
        "top_k": top_k,
        "candidates": {"fts": len(fts_rows), "vector": len(vec_rows)},
        "took_ms": int((time.time() - t0) * 1000),
        "notes": notes,
        "results": results,
    }