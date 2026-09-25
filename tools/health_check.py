r"""kb-mcp 只读体检：索引完整性 + 新鲜度 + 向量覆盖 + 资料库改动情况。

用法：
    .\.venv\Scripts\python.exe tools\health_check.py            # 全部
    .\.venv\Scripts\python.exe tools\health_check.py --brief    # 只看结论

只读：只 SELECT 索引库、只 stat 源库文件，不索引、不写库、不碰源库。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb import config as cfgmod      # noqa: E402
from kb import indexer, store        # noqa: E402
from kb.console import force_utf8_when_piped   # noqa: E402

force_utf8_when_piped()

OK, WARN, BAD = "  ok  ", " warn ", " BAD  "
rows: list[tuple[str, str, str]] = []


def add(state: str, name: str, detail: str = "") -> None:
    rows.append((state, name, detail))


def one(con, sql, *params):
    r = con.execute(sql, params).fetchone()
    return r[0] if r else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    cfg = cfgmod.load()
    db = args.db or cfg["db_path"]
    if not os.path.exists(db):
        print(f"索引库不存在：{db}")
        print("（还没有索引过。第一步状态下这是正常的）")
        return 1
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # ---- 规模 ----
    counts = store.counts(con) if hasattr(store, "counts") else {}
    docs = one(con, "SELECT COUNT(*) FROM docs")
    chunks = one(con, "SELECT COUNT(*) FROM chunks")
    embs = one(con, "SELECT COUNT(*) FROM embeddings")
    fts = one(con, "SELECT COUNT(*) FROM chunks_fts")
    print(f"索引库 {db}")
    print(f"  docs={docs}  chunks={chunks}  fts={fts}  embeddings={embs}"
          f"  文件 {os.path.getsize(db)/1e6:.0f}MB")
    for r in con.execute("SELECT state, COUNT(*) n FROM docs GROUP BY state ORDER BY n DESC"):
        print(f"    {r['state']:<10} {r['n']}")

    # ---- 完整性 ----
    add(BAD if (one(con, "SELECT COUNT(*) FROM chunks") != one(con, "SELECT COUNT(*) FROM chunks_fts"))
        else OK, "chunks 与全文索引行数一致", f"{chunks} vs {fts}")
    orph = one(con, "SELECT COUNT(*) FROM chunks_fts f LEFT JOIN chunks c ON c.id=f.rowid WHERE c.id IS NULL")
    add(BAD if orph else OK, "无孤儿全文行", str(orph))
    novec = one(con, "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
                     "WHERE e.chunk_id IS NULL")
    add(BAD if novec else OK, "每个片段都有向量", f"缺 {novec}")
    add(BAD if one(con, "SELECT COUNT(*) FROM embeddings e LEFT JOIN chunks c ON c.id=e.chunk_id "
                        "WHERE c.id IS NULL") else OK, "无指向空片段的向量")
    bad_dim = one(con, "SELECT COUNT(*) FROM embeddings WHERE dim<>512 OR length(vec)<>512*4")
    add(BAD if bad_dim else OK, "向量维度统一 512", f"异常 {bad_dim}")
    add(BAD if one(con, "SELECT COUNT(*) FROM chunks c JOIN chunks_fts f ON f.rowid=c.id "
                        "WHERE f.text<>c.text") else OK, "全文与片段文本一致")
    add(WARN if one(con, "SELECT COUNT(*) FROM docs d WHERE d.state='indexed' AND "
                         "d.n_chunks <> (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id)") else OK,
        "每篇的片段计数自洽")

    # 向量范数抽样（只抽 400 个，够发现异常，又不拖时间）
    bad_norm = 0
    for r in con.execute("SELECT vec FROM embeddings ORDER BY chunk_id LIMIT 400"):
        v = struct.unpack(f"<{len(r[0])//4}f", r[0])
        if abs(sum(x * x for x in v) ** 0.5 - 1.0) > 1e-3:
            bad_norm += 1
    add(BAD if bad_norm else OK, "向量已 L2 归一化（抽样 400）", f"异常 {bad_norm}")

    # ---- 向量模型一致性 ----
    groups = [dict(r) for r in con.execute(
        "SELECT model, dim, COUNT(*) n FROM embeddings GROUP BY model, dim ORDER BY n DESC")]
    stored = store.meta_get(con, "embed_model", "")
    if len(groups) == 1 and groups[0]["model"] == stored:
        add(OK, "向量只来自一个模型", f"{groups[0]['model']} ({groups[0]['n']})")
    else:
        add(WARN, "向量模型标识不一致", f"meta={stored!r} 实际={groups}")

    # ---- 新鲜度 / 源库状态 ----
    for src in cfg.get("sources", []):
        head_now = indexer.git_head(src["root"])
        head_idx = store.meta_get(con, f"head:{src['id']}", "")
        add(OK if (not head_idx or head_now == head_idx) else WARN,
            f"[{src['id']}] HEAD 与索引时一致", f"{head_now[:12]} vs {head_idx[:12] or '(未记录)'}")
        try:
            diff = indexer.scan_changes(cfg, con)
            per = diff["per_source"].get(src["id"], {})
            add(OK if not diff["stale"] else WARN, f"[{src['id']}] 与索引一致",
                f"文件 {per.get('files', '?')} 新增 {len(diff['new'])} "
                f"改动 {len(diff['changed'])} 删除 {len(diff['removed'])}")
        except Exception as e:
            add(WARN, f"[{src['id']}] 新鲜度检查失败", f"{type(e).__name__}: {e}")

    # ---- 任务与最近索引动作 ----
    print("\n最近后台任务：")
    for r in store.recent_jobs(con, 5):
        print(f"  {r['id']:<22} {r['status']:<12} {r['done']}/{r['total']}  "
              f"{str(r.get('started_at'))[:19]} -> {str(r.get('finished_at') or '')[:19]}")
    print("\n最近索引动作（index_log）：")
    for r in con.execute("SELECT id, source_id, rel_path, action, result, note, ts "
                         "FROM index_log ORDER BY id DESC LIMIT 12"):
        print(f"  {str(r['ts'])[:19]}  {r['action']:<9} {r['result']:<6} "
              f"{os.path.basename(str(r['rel_path'] or ''))[:34]:<36} {str(r['note'] or '')[:60]}")

    # ---- 结论 ----
    if not args.brief:
        print("\n体检明细：")
        for state, name, detail in rows:
            print(f"  {state} {name:<34} {detail}")
    bad = [r for r in rows if r[0] == BAD]
    warn = [r for r in rows if r[0] == WARN]
    print(f"\n结论：{len(rows)} 项检查，{len(bad)} 项异常，{len(warn)} 项提醒")
    for state, name, detail in bad + warn:
        print(f"  {state} {name} {detail}")
    con.close()
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())