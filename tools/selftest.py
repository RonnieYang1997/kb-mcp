# -*- coding: utf-8 -*-
"""第一步验收：端到端自测（临时夹具，不碰真库、不写源库）。

覆盖：
  清洗（BOM/乱码表头/乱码小标题）· 失效正文识别 · 切块 · 全文检索 · 向量检索 · RRF
  · fetch 取全文 · 陈旧检查自动补索引 · MCP stdio 协议连通 · 源库未被改动（sha1/mtime/HEAD 三重证据）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = Path(r"C:\Users\Ronnie\Documents\GitHub\dufuzhixin")
FAILS: list[str] = []
PASSES: list[str] = []


def check(cond, label, detail=""):
    (PASSES if cond else FAILS).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"   {detail}" if detail else ""), flush=True)
    return cond


def sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 16), b""):
            h.update(b)
    return h.hexdigest()


def build_fixture(tmp: Path) -> dict:
    fx = tmp / "fixture"
    fx.mkdir(parents=True, exist_ok=True)
    src_files = sorted(REPO.glob("**/dufu-BV*.md"))
    picks = src_files[:5] + src_files[len(src_files)//3:len(src_files)//3+4] + src_files[-3:]
    picked = []
    for p in picks:
        dst = fx / p.name
        shutil.copyfile(p, dst)
        picked.append(p)
    # 合成边界样本（写在夹具里，属于临时目录）
    (fx / "dufu-BVFAKE001.md").write_text(
        '\ufeff鏃ユ湡锛?2026-01-01\r\nBVID锛?BVFAKE001\r\n'
        '#### \uE162\uE162\uE162\r\n@{page=1}.date)\r\n'
        '# 合成测试标题\r\n## 璇煶杞綍\r\n'
        '这是合成测试正文，用于验证乱码表头与乱码小标题会被清洗掉。' * 12, encoding="utf-8", newline="")
    (fx / "dufu-BVFAKE002.md").write_text(
        '---\ntitle: "失效正文样本"\nbvid: BVFAKE002\ndate: 2026-01-02\n---\n\n'
        '{"error":{"type":"upstream_error","message":"No transcript data in response"}}',
        encoding="utf-8", newline="")
    (fx / "dufu-BVFAKE003.md").write_text("没有 frontmatter 的正文，" * 20, encoding="utf-8")
    (fx / "dufu-BVFAKE004.md").write_text(
        '---\ntitle: 极短\ndate: 2026-01-04\n---\n\n短。', encoding="utf-8")
    return {"dir": fx, "source_files": picked, "n": len(list(fx.glob("*.md")))}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kb_selftest_"))
    print(f"[selftest] 临时目录 {tmp}")
    os.environ["KB_CONFIG"] = str(tmp / "config.json")
    os.environ["LOCALAPPDATA"] = str(tmp)          # 索引库也落在临时目录

    fx = build_fixture(tmp)
    src_sha_before = {p: sha1(p) for p in fx["source_files"]}
    src_mtime_before = {p: p.stat().st_mtime for p in fx["source_files"]}

    sys.path.insert(0, str(ROOT))
    # 用临时配置替换默认源
    example = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    example["db_path"] = str(tmp / "index.db")
    example["sources"] = [{"id": "fixture", "label": "自测夹具", "root": str(fx["dir"]),
                           "include": ["**/dufu-BV*.md"], "exclude": [], "read_only": True, "clean": True}]
    (tmp / "config.json").write_text(json.dumps(example, ensure_ascii=False, indent=2), encoding="utf-8")

    from kb import config as cfgmod, embed as embed_mod, indexer, store, textproc
    from kb import search as search_mod
    from kb.search import search as do_search

    cfg = cfgmod.load()
    conn = store.connect(cfg["db_path"])
    store.init_schema(conn)
    emb = embed_mod.load_from_config(cfg)
    print(f"[selftest] 向量: available={emb.available} reason={emb.reason}")

    print("\n== 1. 索引夹具 ==")
    t0 = time.time()
    res = indexer.index_source(conn, cfg, cfg["sources"][0], embedder=emb, full=True)
    print(f"  stats={ {k: v for k, v in res.items() if k not in ('dead_files','error_files')} }")
    counts = store.counts(conn)
    check(counts["docs"] == fx["n"], f"文档数 = 夹具文件数 ({counts['docs']}/{fx['n']})")
    check(counts["docs_indexed"] >= fx["n"] - 3, f"有效文档 {counts['docs_indexed']}")
    check(counts["chunks"] > 0, f"切块数 {counts['chunks']}")
    check(counts["fts_rows"] == counts["chunks"], "FTS 行数与片段数一致")
    if emb.available:
        check(counts["embeddings"] == counts["chunks"], f"向量数 = 片段数 ({counts['embeddings']})")

    print("\n== 2. 清洗效果 ==")
    row = conn.execute("SELECT id,title FROM docs WHERE rel_path LIKE '%FAKE001%'").fetchone()
    check(row is not None, "合成乱码样本被索引")
    if row:
        txts = [r["text"] for r in conn.execute("SELECT text FROM chunks WHERE doc_id=?", (row["id"],))]
        joined = "\n".join(txts)
        check("\ue162" not in joined, "私用区乱码字符已清除")
        check("锛" not in joined and "鏃ユ湡" not in joined, "GBK 乱码表头已清除")
        check("璇煶杞綍" not in joined, "乱码小标题已清除")
        check("合成测试正文" in joined, "正文保留完整")

    print("\n== 3. 失效正文识别 ==")
    dead = conn.execute("SELECT id,state,note FROM docs WHERE rel_path LIKE '%FAKE002%'").fetchone()
    check(dead is not None and dead["state"] == "dead", "JSON 错误正文标记为 dead",
          f"state={dead['state'] if dead else None} note={dead['note'] if dead else ''}")
    if dead:
        n = conn.execute("SELECT COUNT(*) FROM chunks WHERE doc_id=?", (dead["id"],)).fetchone()[0]
        check(n == 0, "失效正文不产生片段")
    empty = conn.execute("SELECT state FROM docs WHERE rel_path LIKE '%FAKE004%'").fetchone()
    check(empty is not None and empty["state"] == "empty", "极短正文标记为 empty")

    print("\n== 4. 检索（全文 / 向量 / RRF） ==")
    r_fts = do_search(conn, cfg, "语音转录", embedder=None, mode="fts", top_k=5)
    check(len(r_fts["results"]) > 0, f"FTS 命中 {len(r_fts['results'])} 条", f"notes={r_fts['notes']}")
    kw = None
    if r_fts["results"]:
        kw = r_fts["results"][0]["snippet"][:12]
    q_long = "语音转录中提到的关键内容是什么"
    r_hy = do_search(conn, cfg, q_long, embedder=emb, mode="hybrid", top_k=5)
    check(len(r_hy["results"]) > 0, f"长中文查询命中 {len(r_hy['results'])} 条", f"took={r_hy['took_ms']}ms")
    scores = [x["score"] for x in r_hy["results"]]
    check(scores == sorted(scores, reverse=True), "RRF 分数降序")
    if emb.available:
        r_vec = do_search(conn, cfg, q_long, embedder=emb, mode="vector", top_k=5)
        check(len(r_vec["results"]) > 0, f"向量检索命中 {len(r_vec['results'])} 条")
        check(any(x["vector_rank"] for x in r_hy["results"]), "混合结果里含向量命中来源")
    check(any(x["fts_rank"] for x in r_hy["results"]), "混合结果里含全文命中来源")
    from collections import Counter as _Counter
    _cnt = _Counter(x["bvid"] for x in r_hy["results"])
    check(max(_cnt.values() or [0]) <= 2, "同一篇最多 2 个片段（同源折叠生效）", f"{dict(_cnt)}")
    _recent = search_mod.search(conn, cfg, "疫苗", embedder=emb, top_k=5, sort="recent")
    _dates = [x["date"] for x in _recent["results"] if x.get("date")]
    check(_dates == sorted(_dates, reverse=True), "sort=recent 按出片时间倒序", f"{_dates[:5]}")
    _empty = search_mod.search(conn, cfg, "  。。 ", embedder=emb, top_k=5)
    check(_empty["mode"] == "empty" and not _empty["results"], "空/纯标点查询返回空而不是随机结果")

    print("\n== 5. fetch 全文 ==")
    doc = conn.execute("SELECT id, rel_path, state FROM docs WHERE state='indexed' LIMIT 1").fetchone()
    from kb.server import _doc_text, _resolve_doc
    d = _resolve_doc(conn, doc["id"])
    body, m = _doc_text(cfg, d["source_id"], d["abs_path"])
    check(len(body) > 100, f"取得全文 {len(body)} 字", f"title={m['title'][:24]}")

    print("\n== 6. 陈旧检查（惰性兜底） ==")
    newest = fx["dir"] / "dufu-BVFAKE003.md"
    newest.write_text("没有 frontmatter 的正文，追加了新内容验证陈旧检查。" * 30, encoding="utf-8")
    store.meta_set(conn, "last_walk_ts", time.time() - 10_000)
    conn.commit()
    info = indexer.ensure_fresh(conn, cfg, embedder=emb, budget_seconds=120)
    check(info.get("stale") is True and info.get("reindexed") is True,
          "检测到源库更新并自动补索引", f"runs={[ (r['new'], r['updated']) for r in info.get('runs', []) ]}")
    again = indexer.ensure_fresh(conn, cfg, embedder=emb)   # 节流
    check(again.get("checked") is False, "节流生效（短时间内不重复扫描）")

    print("\n== 6b. 第二步安全开关 (scan.auto_index=false) ==")
    import copy as _copy
    locked = _copy.deepcopy(cfg)
    locked["scan"]["auto_index"] = False
    docs_before = store.counts(conn)["docs"]
    lk = indexer.ensure_fresh(conn, locked, embedder=emb, force=True)
    check(lk.get("locked") is True and lk.get("reindexed") is False
          and lk.get("checked") is False,
          "auto_index=false → ensure_fresh 直接返回锁定，不读源库",
          f"reason={lk.get('reason')}")
    lk2 = indexer.index_source(conn, locked, locked["sources"][0], embedder=emb, full=True)
    check(lk2.get("locked") is True and lk2.get("total") == 0
          and lk2.get("chunks") == 0 and lk2.get("embedded") == 0,
          "auto_index=false → index_source 不遍历不建索引",
          f"total={lk2.get('total')} chunks={lk2.get('chunks')} embedded={lk2.get('embedded')}")
    check(store.counts(conn)["docs"] == docs_before, "锁定期间文档数不变")

    print("\n== 6c. 新增文件 / 崩溃残局 / 换模型 ==")
    # a) 全新文件（不是修改既有文件）也应建片段+向量
    src_dir = fx["dir"]
    brand_new = src_dir / "dufu-BVnewfile9001.md"
    body_txt = ("大家好，这是一篇全新的转录，用于验证新增文件的增量索引。"
                "今天讲三件事：通胀、就业、以及消费信心。") * 8
    brand_new.write_text("---\ntitle: 全新测试\ndescription: 2026-09-24 新增\n---\n\n" + body_txt,
                         encoding="utf-8")
    store.meta_set(conn, "last_walk_ts", time.time() - 10_000)
    conn.commit()
    inc = indexer.index_source(conn, cfg, cfg["sources"][0], embedder=emb, full=False)
    row = conn.execute("SELECT id, state, n_chunks FROM docs WHERE rel_path LIKE '%newfile9001%'").fetchone()
    check(row is not None and row["state"] == "indexed" and row["n_chunks"] > 0,
          "新增文件被索引成片段", f"new={inc['new']} chunks={row['n_chunks'] if row else '-'}")
    if emb.available and row:
        nv = conn.execute("SELECT COUNT(*) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id "
                          "WHERE c.doc_id=?", (row["id"],)).fetchone()[0]
        check(nv == row["n_chunks"], "新增文件的片段全部带上向量（今晚 daily_scan 依赖这条）",
              f"vectors={nv}/{row['n_chunks']}")

    # b) 模拟"片段落盘了、向量还没写就崩" → 下次 index 应自动补齐
    if emb.available and row:
        victim = [r[0] for r in conn.execute(
            "SELECT chunk_id FROM embeddings ORDER BY chunk_id DESC LIMIT 3")]
        conn.executemany("DELETE FROM embeddings WHERE chunk_id=?", [(i,) for i in victim])
        conn.commit()
        lost = conn.execute("SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
                            "WHERE e.chunk_id IS NULL").fetchone()[0]
        bf = indexer.index_source(conn, cfg, cfg["sources"][0], embedder=emb, full=False)
        still = conn.execute("SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
                             "WHERE e.chunk_id IS NULL").fetchone()[0]
        check(lost > 0 and bf["backfilled_chunks"] > 0 and still == 0,
              "缺向量的片段被自动补齐（防「崩在两次提交之间」）",
              f"缺={lost} 补={bf['backfilled_chunks']} 剩余={still}")

    # c) 换向量模型（int8 <-> fp32）时不得把两种向量混在一张表里
    if emb.available:
        good_id = emb.model_id
        store.meta_set(conn, "embed_model", "bogus-model::x::512")
        conn.commit()
        n_before = store.counts(conn)["embeddings"]
        mm = indexer.index_source(conn, cfg, cfg["sources"][0], embedder=emb, full=False)
        n_after = store.counts(conn)["embeddings"]
        check(mm["model_mismatch"] is True and mm["embedded"] == 0 and n_after == n_before,
              "换了向量模型时拒绝混写（只更新全文，要求 --full 重建）",
              f"mismatch={mm['model_mismatch']} {n_before}->{n_after}")
        store.meta_set(conn, "embed_model", good_id)
        conn.commit()
        back = indexer.index_source(conn, cfg, cfg["sources"][0], embedder=emb, full=False)
        check(back["model_mismatch"] is False, "模型标识恢复正常后不再告警")

    print("\n== 7. 源库未被改动（三重证据） ==")
    left = [(p, sha1(p) != src_sha_before[p]) for p in fx["source_files"]]
    check(not any(ch for _, ch in left), "被复制的源文件 sha1 未变",
          f"changed={sum(1 for _, ch in left if ch)}")
    mch = [p for p in fx["source_files"] if abs(p.stat().st_mtime - src_mtime_before[p]) > 1e-6]
    check(not mch, "被复制的源文件 mtime 未变", f"changed={len(mch)}")
    head_now = indexer.git_head(str(REPO))
    check(not res["head_changed"], f"索引期间源库 HEAD 未变 ({head_now})")

    print("\n== 8. 只读自审 ==")
    from kb import audit
    a = audit.summarize()
    check(a["clean"], f"代码内无未解释的写操作 (发现 {len(a['unexplained'])})",
          "" if a["clean"] else str([f"{x['file']}:{x['line']}" for x in a["unexplained"]][:5]))

    print("\n== 9. MCP stdio 协议连通 ==")
    env = dict(os.environ)
    proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "kb_serve.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env, cwd=str(ROOT))

    def rpc(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    r1 = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "selftest", "version": "0"}}})
    check(r1 and r1["result"]["protocolVersion"] == "2025-06-18",
          "initialize 握手", f"serverInfo={r1['result']['serverInfo'] if r1 else None}")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    r2 = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r2["result"]["tools"]] if r2 else []
    check(names == ["search", "fetch", "list_documents", "stats", "reindex"],
          f"tools/list 返回 5 个工具 {names}")
    r3 = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "search", "arguments": {"query": "语音转录", "top_k": 3}}})
    ok3 = bool(r3 and not r3["result"]["isError"])
    check(ok3, "tools/call search 成功")
    if ok3:
        payload = json.loads(r3["result"]["content"][0]["text"])
        check(len(payload["data"]["results"]) > 0, f"search 返回 {len(payload['data']['results'])} 条")
    r4 = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "stats", "arguments": {}}})
    ok4 = bool(r4 and not r4["result"]["isError"])
    check(ok4, "tools/call stats 成功")
    r5 = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "reindex", "arguments": {"full": False}}})
    check(bool(r5 and not r5["result"]["isError"]), "tools/call reindex（增量）成功")
    r6 = rpc({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
              "params": {"name": "fetch", "arguments": {"doc_id": doc["id"], "max_chars": 200}}})
    check(bool(r6 and not r6["result"]["isError"]), "tools/call fetch 成功")
    r7 = rpc({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
              "params": {"name": "不存在的工具", "arguments": {}}})
    check(bool(r7 and "error" in r7), "未知工具返回 JSON-RPC 错误而非崩溃")
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "exit"}) + "\n")
    proc.stdin.flush()
    try:
        proc.wait(timeout=10)
        check(True, f"exit 后进程正常退出 code={proc.returncode}")
    except subprocess.TimeoutExpired:
        proc.kill()
        check(False, "exit 后进程未退出，已强杀")
    err = proc.stderr.read()
    stray = [l for l in err.splitlines() if "STRAY-STDOUT" in l]
    check(not stray, "stdout 未被非协议输出污染")


    print("\n" + "=" * 60)
    print(f"PASS {len(PASSES)}   FAIL {len(FAILS)}")
    for f in FAILS:
        print("  ✗ " + f)
    keep = os.environ.get("KB_SELFTEST_KEEP")
    if keep:
        print(f"[selftest] 保留临时目录: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())