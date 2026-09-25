# -*- coding: utf-8 -*-
r"""kb-mcp 端到端验证：直接调用 kb-mcp，能不能真的拿到「已经向量化的内容」。

这个脚本回答的就是 Ronnie 2026-09-25 那个问题，把它变成一条可重复执行的命令，
不用再靠我口头保证：

    1. 走真 MCP stdio（和 AionUi 用的同一条链路）：initialize → tools/list → tools/call
       —— 数据是从 `bin/kb_serve.py` 子进程问出来的，不是我直接读库读出来的
    2. 索引完整性对账：chunks / embeddings / chunks_fts 三者 1:1、无缺向量、模型维度统一
    3. 向量臂到底有没有在贡献：用 search **自己返回的** fts_rank / vector_rank 判定，
       `fts_rank=null + vector_rank=N` 的结果只可能来自向量臂
    4. 终局举证：拿一条「仅向量命中」的结果，用它的 chunk_id 从 embeddings 表取 blob，
       手工复算余弦，与 search 报的 cosine 对齐 —— 对得上，命中就确实来自存量向量
    5. 降级是否诚实：向量不可用时，是否明说「向量检索不可用」，而不是假装命中

用法：
    .\.venv\Scripts\python.exe tools\e2e_check.py            # 全部
    .\.venv\Scripts\python.exe tools\e2e_check.py --brief    # 只看结论

Windows 重定向注意（PowerShell 5.1 会按 cp936 解码子进程 stdout，中文会乱码）：
    [Console]::OutputEncoding = [Text.Encoding]::UTF8
    .\.venv\Scripts\python.exe tools\e2e_check.py

只读：只 SELECT 索引库；MCP 子进程也只用 stats / search，不索引、不写库、不碰源库。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kb import config as cfgmod          # noqa: E402
from kb import embed as embed_mod        # noqa: E402
from kb import search as search_mod      # noqa: E402
from kb import store                     # noqa: E402
from kb.console import force_utf8_when_piped   # noqa: E402

force_utf8_when_piped()

OK, WARN, BAD = "  ok  ", " warn ", " BAD  "
rows: list[tuple[str, str, str]] = []

QUERIES = [
    "为什么很多人相信外国的东西一定比我们的好",
    "下雨天路面全泡在水里出不去门",
    "被人断供之后还能不能自己造出来",
    "老百姓手里的钱越来越不经花",
    "德国在青岛修的下水道",
    "中国基建是不是吹出来的",
]


def add(state: str, name: str, detail: str = "") -> None:
    rows.append((state, name, detail))


def one(con, sql, *params):
    r = con.execute(sql, params).fetchone()
    return r[0] if r else None


# --------------------------------------------------------------- 1. MCP 协议层
class Mcp:
    """最小 stdio JSON-RPC 客户端，尽量照 MCP 的真实交互来。"""

    def __init__(self) -> None:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "kb_serve.py")], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace", bufsize=1)

    def call(self, obj: dict, want_id, timeout: float = 300):
        assert self.p.stdin and self.p.stdout
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()
        t0 = time.time()
        while time.time() - t0 < timeout:
            raw = self.p.stdout.readline()
            if not raw:
                raise RuntimeError("MCP 服务端 stdout 已关闭（看下面 stderr）")
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if want_id is None or msg.get("id") == want_id:
                return msg
        raise TimeoutError("等 MCP 响应超时")

    def notify(self, obj: dict) -> None:
        assert self.p.stdin
        self.p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.p.stdin.flush()

    def close(self) -> str:
        try:
            if self.p.stdin:
                self.p.stdin.close()
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()
        return (self.p.stderr.read() or "").strip() if self.p.stderr else ""


def mcp_probe(brief: bool) -> tuple[dict, dict]:
    print("【1】MCP 协议层：真的调 kb-mcp（stdio 子进程，和 AionUi 同一条链路）")
    m = Mcp()
    stats_data: dict = {}
    search_info: dict = {}
    try:
        r = m.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e_check", "version": "1"}}}, 1)
        info = r["result"]
        print(f"  握手 OK：{info['serverInfo']['name']} v{info['serverInfo']['version']}"
              f"  protocol={info['protocolVersion']}")
        add(OK, "MCP 握手成功", f"protocol={info['protocolVersion']}")

        m.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tl = m.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 2)
        names = [t["name"] for t in tl["result"]["tools"]]
        print(f"  tools/list → {len(names)} 个：{', '.join(names)}")
        add(OK if {"search", "fetch", "verify_quotes", "stats"} <= set(names) else BAD,
            "MCP 工具齐全", f"{len(names)} 个 {names}")

        st = m.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "stats", "arguments": {}}}, 3)
        payload = json.loads(st["result"]["content"][0]["text"])
        stats_data = payload.get("data") or {}
        c = stats_data.get("counts", {})
        print(f"  tools/call stats：db={stats_data.get('db_path')}")
        print(f"    docs={c.get('docs')} (indexed {c.get('docs_indexed')})"
              f"  chunks={c.get('chunks')}  embeddings={c.get('embeddings')}"
              f"  fts_rows={c.get('fts_rows')}")
        print(f"    stale={stats_data.get('stale')}"
              f"  pending={stats_data.get('pending_changes')}"
              f"  last_index={stats_data.get('last_index_ts')}")
        add(OK if not st["result"].get("isError") else BAD, "tools/call stats 可用",
            f"chunks={c.get('chunks')} embeddings={c.get('embeddings')}")

        for i, q in enumerate(QUERIES[:2]):
            rid = 10 + i
            rr = m.call({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                         "params": {"name": "search",
                                    "arguments": {"query": q, "top_k": 5}}}, rid)
            d = json.loads(rr["result"]["content"][0]["text"])["data"]
            search_info[q] = d
            print(f"\n  search 「{q}」")
            print(f"    mode={d['mode']}  took={d['took_ms']}ms  candidates={d['candidates']}")
            if d.get("notes"):
                print(f"    notes={d['notes']}")
            for it in d["results"]:
                print(f"    #{it['rank']} {it['bvid']} {it['date']}"
                      f"  fts={it['fts_rank']} vec={it['vector_rank']} cos={it['cosine']}")
        modes = {d["mode"] for d in search_info.values()}
        add(OK if modes == {"hybrid"} else WARN, "经 MCP 的 search 两臂都在跑",
            f"mode={sorted(modes)}")
    except Exception as e:
        add(BAD, "MCP 协议层调用失败", f"{type(e).__name__}: {e}")
        print(f"  !! {type(e).__name__}: {e}")
    finally:
        err = m.close()
        if err:
            print("\n  [服务端 stderr]\n    " + err.replace("\n", "\n    "))
    return stats_data, search_info


# --------------------------------------------------------------- 2. 索引对账
def db_probe() -> dict:
    print("\n【2】索引完整性：向量是不是真的落库了（只读打开索引库）")
    db = cfgmod.load()["db_path"]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    out = {
        "docs": one(con, "SELECT COUNT(*) FROM docs"),
        "chunks": one(con, "SELECT COUNT(*) FROM chunks"),
        "embeddings": one(con, "SELECT COUNT(*) FROM embeddings"),
        "fts_rows": one(con, "SELECT COUNT(*) FROM chunks_fts"),
    }
    print(f"  docs={out['docs']}  chunks={out['chunks']}"
          f"  embeddings={out['embeddings']}  fts_rows={out['fts_rows']}")
    for r in con.execute("SELECT state, COUNT(*) n FROM docs GROUP BY state ORDER BY n DESC"):
        print(f"    {r['state']:<10} {r['n']}")

    same = out["chunks"] == out["embeddings"] == out["fts_rows"]
    add(OK if same else BAD, "chunks == embeddings == fts_rows",
        f"{out['chunks']} / {out['embeddings']} / {out['fts_rows']}")
    miss_e = one(con, "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
                      "WHERE e.chunk_id IS NULL")
    miss_f = one(con, "SELECT COUNT(*) FROM chunks c LEFT JOIN chunks_fts f ON f.rowid=c.id "
                      "WHERE f.rowid IS NULL")
    add(OK if not miss_e else BAD, "每个片段都有向量", f"缺 {miss_e}")
    add(OK if not miss_f else BAD, "每个片段都有全文行", f"缺 {miss_f}")

    groups = [dict(r) for r in con.execute(
        "SELECT model, dim, COUNT(*) n FROM embeddings GROUP BY model, dim ORDER BY n DESC")]
    print("  向量分组：")
    for g in groups:
        print(f"    {g['n']:>7}  {g['model']}  dim={g['dim']}")
    add(OK if len(groups) == 1 and groups[0]["dim"] == 512 else BAD,
        "向量只有一组（模型+维度）", f"{groups}")
    print("  meta：")
    for r in con.execute("SELECT key, value FROM meta ORDER BY key"):
        print(f"    {r['key']:<22} = {str(r['value'])[:70]}")

    st = os.stat(db)
    print(f"  库文件：{db}\n    {st.st_size / 1048576:.1f} MB，最后写入 "
          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}")
    con.close()
    return out


# --------------------------------------------------------- 3.+4. 两臂与举证
def arm_probe(brief: bool) -> None:
    print("\n【3】向量臂有没有在贡献：看 search 自己报的 fts_rank / vector_rank")
    cfg = cfgmod.load()
    emb = embed_mod.load_from_config(cfg)
    prefix = cfg.get("embed", {}).get("query_prefix", "")
    available = bool(getattr(emb, "available", False))
    print(f"  向量模型：available={available}"
          f"  query_prefix={prefix!r}  doc_prefix={cfg.get('embed', {}).get('doc_prefix', '')!r}")
    add(OK if available else BAD, "查询时向量模型能加载",
        f"available={available} {getattr(emb, 'reason', '') or ''}")

    conn = store.connect(cfg["db_path"])
    tot, showcase, rows_txt = Counter(), None, []
    for q in QUERIES:
        d = search_mod.search(conn, cfg, q, embedder=emb, top_k=50)
        cnt = Counter()
        for it in d["results"]:
            f, v = it["fts_rank"] is not None, it["vector_rank"] is not None
            cnt["两臂都命中" if (f and v) else "仅全文命中" if f else
                "仅向量命中" if v else "都没标"] += 1
            if v and not f and showcase is None:
                showcase = (q, dict(it))
        tot.update(cnt)
        rows_txt.append((q, d["mode"], d["candidates"]["fts"], d["candidates"]["vector"],
                         cnt["仅向量命中"], cnt["仅全文命中"], cnt["两臂都命中"]))

    print(f"\n  {'查询':<24}{'模式':<8}{'FTS候选':>8}{'向量候选':>9}"
          f"{'仅向量':>7}{'仅全文':>7}{'两臂':>6}")
    for q, mode, f, v, vo, fo, both in rows_txt:
        print(f"  {q[:22]:<24}{mode:<8}{f:>8}{v:>9}{vo:>7}{fo:>7}{both:>6}")
    print(f"\n  前 50 名合计：{dict(tot)}")
    print("  「仅向量命中」= 全文侧前 200 名里根本没有它，只可能是向量臂捞出来的。")
    add(OK if tot["仅向量命中"] else BAD, "存在『仅向量命中』的结果",
        f"{tot['仅向量命中']} 条 / 共 {sum(tot.values())} 条")

    if showcase:
        q, it = showcase
        print(f"\n【4】终局举证：手工复算一条『仅向量命中』的余弦")
        print(f"  查询「{q}」")
        print(f"  search 报：bvid={it['bvid']} chunk_id={it['chunk_id']}"
              f"  vector_rank={it['vector_rank']}  fts_rank={it['fts_rank']}"
              f"  cosine={it['cosine']}")
        try:
            ro = sqlite3.connect(f"file:{cfg['db_path']}?mode=ro", uri=True)
            blob, dim, model = ro.execute(
                "SELECT vec, dim, model FROM embeddings WHERE chunk_id=?",
                (it["chunk_id"],)).fetchone()
            v = np.frombuffer(blob, dtype=np.float32)
            qv = np.asarray(emb.encode([q], prefix=prefix)[0], dtype=np.float32)
            cos = float(qv @ v / (np.linalg.norm(qv) * np.linalg.norm(v)))
            txt = ro.execute("SELECT text FROM chunks WHERE id=?",
                             (it["chunk_id"],)).fetchone()[0]
            ro.close()
            print(f"  库里 blob：{len(blob)} 字节 = {len(blob) // 4} 个 float32"
                  f"  dim={dim}  model={model}")
            print(f"  手工复算 cos(查询, 存量向量) = {cos:.4f}"
                  f"   与 search 报的 {it['cosine']} 差 {abs(cos - it['cosine']):.6f}")
            print(f"  该片段正文 {len(txt)} 字：{txt[:80]}…")
            add(OK if abs(cos - it["cosine"]) < 1e-3 else BAD,
                "手工复算的余弦与 search 报的一致",
                f"{cos:.4f} vs {it['cosine']}（命中确实来自存量向量）")
        except Exception as e:
            add(BAD, "手工复算余弦失败", f"{type(e).__name__}: {e}")
    else:
        add(WARN, "本轮没找到『仅向量命中』的样例，跳过举证", "可换一批查询再试")

    print("\n【5】向量不可用时，会不会假装命中")
    class DeadEmb:
        available = False
        reason = "e2e_check: 故意关掉"

    d = search_mod.search(conn, cfg, "德国的下水道", embedder=DeadEmb(), top_k=3)
    conn.close()
    honest = (d["mode"] == "fts" and not d["candidates"]["vector"]
              and any("不可用" in n for n in d["notes"]))
    print(f"  模式：hybrid → {d['mode']}  vector候选={d['candidates']['vector']}")
    for n in d["notes"]:
        print(f"    note: {n}")
    add(OK if honest else BAD, "向量不可用时如实降级并说明", f"mode={d['mode']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true")
    args = ap.parse_args()

    stats_data, _ = mcp_probe(args.brief)
    db = db_probe()
    if stats_data.get("counts"):
        c = stats_data["counts"]
        add(OK if (c.get("chunks") == db["chunks"]
                   and c.get("embeddings") == db["embeddings"]) else BAD,
            "经 MCP 问到的库 = 直连索引库",
            f"MCP {c.get('chunks')}/{c.get('embeddings')} vs "
            f"直连 {db['chunks']}/{db['embeddings']}")

    arm_probe(args.brief)

    if not args.brief:
        print("\n明细：")
        for state, name, detail in rows:
            print(f"  {state} {name:<34} {detail}")
    bad = [r for r in rows if r[0] == BAD]
    warn = [r for r in rows if r[0] == WARN]
    print(f"\n结论：{len(rows)} 项，{len(bad)} 项异常，{len(warn)} 项提醒")
    for state, name, detail in bad + warn:
        print(f"  {state} {name} {detail}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())