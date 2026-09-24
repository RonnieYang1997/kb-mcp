# -*- coding: utf-8 -*-
"""MCP stdio 服务：换行分隔的 JSON-RPC 2.0。

可靠性设计（针对 KMCP 的失败点）：
- 标准库实现，零第三方协议依赖；
- stdout 只走协议，任何第三方 print() 会被 StdoutGuard 拦下并写到 stderr；
- 每次读写都 try/except，单个文件/单个工具出错不会让服务崩掉；
- 工具返回里带 notes/耗时/索引新鲜度，失败不静默。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import __version__, config as cfgmod, embed as embed_mod, indexer, search as search_mod, store

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL = "2024-11-05"
ROOT = cfgmod.ROOT

_LOG_FH = None


def log(msg: str) -> None:
    line = f"[kb-mcp {time.strftime('%H:%M:%S')}] {msg}"
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass
    if _LOG_FH:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception:
            pass


class StdoutGuard(io.TextIOBase):
    """挡住任何非协议的 stdout 写入，避免污染 MCP 通道。"""

    def __init__(self, real):
        self._real = real

    def write(self, s):  # type: ignore[override]
        if s and s.strip():
            log("STRAY-STDOUT: " + s.strip()[:300])
        return len(s or "")

    def flush(self):  # type: ignore[override]
        return None

    def isatty(self):  # type: ignore[override]
        return False


# ---------- 工具定义 ----------

def tool_defs() -> list[dict]:
    return [
        {
            "name": "search",
            "description": ("在本地转录语料里做混合检索（中文全文 + 向量，RRF 融合）。"
                            "返回带出处的片段：标题/日期/BVID/文件路径/片段序号，"
                            "每条结果都带 chunk_id 与 doc_id。"
                            "要看完整上下文就把 chunk_id 交给 fetch（会自动对准命中位置）。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索问题或关键词（中文长句可以直接给）"},
                    "top_k": {"type": "integer", "description": "返回片段数，默认 8，上限 50"},
                    "source": {"type": "string", "description": "只在某个资料库内检索（库 id）"},
                    "mode": {"type": "string", "enum": ["hybrid", "fts", "vector"],
                             "description": "hybrid=全文+向量（默认）；fts=只全文；vector=只向量"},
                    "date_from": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "fetch",
            "description": ("取一篇转录的完整正文（已做只读清洗），用于整理与总结。"
                            "最常用：把 search 结果里的 chunk_id 丢进来，会自动定位到该片段所在文档、"
                            "并把返回窗口对准命中位置（前后各留上下文）。也支持 doc_id / 路径 / BVID。"
                            "大文件可配合 offset/max_chars 分段取。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "integer",
                                 "description": "search 结果里的 chunk_id，最推荐的用法"},
                    "doc_id": {"type": "integer", "description": "search/list_documents 返回的 doc_id"},
                    "path": {"type": "string", "description": "相对资料库的路径，如 memory/dufu-BV1xx.md"},
                    "bvid": {"type": "string", "description": "BVID，如 BV1JUN76wECw"},
                    "offset": {"type": "integer", "description": "从第几个字符开始，默认 0"},
                    "max_chars": {"type": "integer", "description": "最多返回多少字，默认 20000"},
                },
            },
        },
        {
            "name": "list_documents",
            "description": "按日期/标题/BVID/资料库枚举已索引的转录，用于挑出要做整理或总结的清单。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "title_contains": {"type": "string"},
                    "bvid_contains": {"type": "string"},
                    "include_dead": {"type": "boolean", "description": "是否包含正文失效的条目，默认 false"},
                    "order": {"type": "string", "enum": ["date_desc", "date_asc", "title"], "description": "默认 date_desc"},
                    "limit": {"type": "integer", "description": "默认 50，上限 200"},
                    "offset": {"type": "integer", "description": "默认 0"},
                },
            },
        },
        {
            "name": "stats",
            "description": "索引与运行状态：文档/片段/向量数量、各资料库文件数与最新修改时间、索引是否过期、模型是否就绪、最近索引任务。",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "reindex",
            "description": ("刷新索引。默认增量（只处理新增/改动/删除的文件）；"
                            "full=true 走后台全量重建（耗时较久，返回 job id，用 stats 查进度）。"
                            "只写索引库，绝不改动资料库。"),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "full": {"type": "boolean", "description": "是否全量重建，默认 false"},
                    "source": {"type": "string", "description": "只刷新某个库"},
                    "background": {"type": "boolean", "description": "full 时是否后台执行，默认 true"},
                    "wait_seconds": {"type": "integer", "description": "增量刷新最长等待秒数，默认 60"},
                },
            },
        },
    ]


# ---------- 工具参数：宽容但绝不含糊 ----------

def _as_bool(v, default: bool = False) -> bool:
    """严格布尔：'false'/'0'/'no' 都是 False。

    不能直接 bool(v)——字符串 "false" 是 truthy，一次手滑就会误触发 2 小时的全量重建。
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "off", "n", ""):
            return False
        if s in ("true", "1", "yes", "on", "y"):
            return True
    return default


def _as_int(v, default: int, lo: int | None = None, hi: int | None = None) -> int:
    """容错取整：'abc' / null / 越界都不会把 Python 异常泄漏给调用方。"""
    if v is None or isinstance(v, bool):
        n = default
    elif isinstance(v, (int, float)):
        n = int(v)
    else:
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


VALID_MODES = ("hybrid", "fts", "vector")


def _as_mode(v, notes: list) -> str:
    if not v:
        return "hybrid"
    s = str(v).strip().lower()
    if s in VALID_MODES:
        return s
    notes.append(f"未知 mode={v!r}，已按 hybrid 处理（可选：{'/'.join(VALID_MODES)}）")
    return "hybrid"


# ---------- 工具实现 ----------

class Ctx:
    def __init__(self, cfg, conn, embedder):
        self.cfg = cfg
        self.conn = conn
        self.embedder = embedder
        self.fresh_info = None

    def ensure_fresh(self, budget: float = 25.0) -> dict:
        try:
            info = indexer.ensure_fresh(self.conn, self.cfg, embedder=self.embedder,
                                        budget_seconds=budget)
        except Exception as e:
            log("ensure_fresh 失败: " + traceback.format_exc(limit=2))
            return {"checked": False, "error": f"{type(e).__name__}: {e}"}
        self.fresh_info = info
        return info


def _resolve_doc(conn, doc_id=None, path=None, bvid=None) -> dict | None:
    if doc_id:
        r = conn.execute("SELECT * FROM docs WHERE id=?", (int(doc_id),)).fetchone()
        return dict(r) if r else None
    if path:
        p = str(path).replace("\\", "/")
        r = conn.execute("SELECT * FROM docs WHERE rel_path=? OR rel_path LIKE ? LIMIT 1",
                         (p, "%" + p.lstrip("/"))).fetchone()
        return dict(r) if r else None
    if bvid:
        r = conn.execute("SELECT * FROM docs WHERE bvid=? ORDER BY date DESC LIMIT 1", (bvid,)).fetchone()
        return dict(r) if r else None
    return None


def _doc_text(cfg, src_id: str, abs_path: str) -> str:
    """重新只读读取并清洗正文（索引库存的是片段，全文本这里现取）。"""
    from . import textproc
    with open(abs_path, "rb") as f:      # 只读
        raw = f.read()
    text = raw.decode("utf-8", errors="replace")
    meta, body, _ = textproc.parse_front_matter(text)
    m = textproc.extract_meta(meta, body, os.path.basename(abs_path))
    clean = True
    for s in cfg.get("sources", []):
        if s["id"] == src_id:
            clean = bool(s.get("clean", True))
    body_clean, _stats = textproc.clean_body(body, m["title"], clean)
    return body_clean, m


def t_search(ctx: Ctx, args: dict) -> dict:
    ctx.ensure_fresh()
    s_cfg = ctx.cfg.get("search", {})
    notes: list = []
    top_k = _as_int(args.get("top_k"), int(s_cfg.get("default_top_k", 8)), 1,
                    int(s_cfg.get("max_top_k", 50)))
    res = search_mod.search(ctx.conn, ctx.cfg, str(args.get("query", "")), embedder=ctx.embedder,
                            top_k=top_k, source=args.get("source"),
                            mode=_as_mode(args.get("mode"), notes),
                            date_from=args.get("date_from"), date_to=args.get("date_to"))
    if notes:
        res.setdefault("notes", []).extend(notes)
    res["index"] = _fresh_brief(ctx)
    if not res["results"]:
        res["hint"] = "没有命中。可尝试缩短查询词、用 mode=fts，或先调 reindex 确认索引已建。"
    return res


def _fresh_brief(ctx: Ctx) -> dict:
    info = ctx.fresh_info or {}
    brief = {"reindexed": info.get("reindexed", False)}
    if info.get("busy_job"):
        brief["pending"] = True
        brief["stale_check"] = f"后台任务 {info['busy_job']} 正在建索引，本次未抢写锁"
    elif info.get("locked"):
        brief["locked"] = True
        brief["stale_check"] = indexer.LOCK_MESSAGE
    elif info.get("checked") is False:
        brief["stale_check"] = "节流跳过（%.0fs 内已检查过）" % info.get("seconds_since_walk", 0)
    return brief


def t_fetch(ctx: Ctx, args: dict) -> dict:
    # 支持直接用 search 返回的 chunk_id：自动定位到该片段所在文档，并把正文窗口对准这个片段
    cid = args.get("chunk_id")
    doc = None
    anchor = ""
    chunk_seq = None
    if cid:
        try:
            m = store.doc_of_chunk(ctx.conn, [int(cid)]).get(int(cid))
        except (TypeError, ValueError):
            m = None
        if not m:
            return {"error": f"chunk_id={cid} 不存在（可能是旧索引，请重新 search）"}
        doc = _resolve_doc(ctx.conn, m["doc_id"])
        anchor = (m.get("text") or "")[:60]
        chunk_seq = m.get("seq")
    if not doc:
        doc = _resolve_doc(ctx.conn, args.get("doc_id"), args.get("path"), args.get("bvid"))
    if not doc:
        return {"error": "未找到对应文档；请先用 search 或 list_documents 取 chunk_id/doc_id/path/bvid"}
    max_chars = _as_int(args.get("max_chars"), 20000, 1, 2_000_000)
    offset = _as_int(args.get("offset"), 0, 0)
    if not os.path.exists(doc["abs_path"]):
        return {"error": f"源文件已不存在: {doc['rel_path']}（可能已被移动，索引需要刷新）"}
    body, m = _doc_text(ctx.cfg, doc["source_id"], doc["abs_path"])
    if anchor:
        idx = body.find(anchor)
        if idx >= 0:
            offset = max(0, idx - min(120, max_chars // 3))   # 片段前留一点上下文
    piece = body[offset:offset + max_chars]
    return {
        "doc_id": doc["id"], "chunk_id": int(cid) if cid else None, "chunk_seq": chunk_seq,
        "title": doc["title"], "bvid": doc["bvid"], "date": doc["date"],
        "duration": doc["duration"], "source": doc["source_id"], "path": doc["rel_path"],
        "state": doc["state"], "total_chars": len(body), "offset": offset,
        "returned_chars": len(piece), "has_more": offset + max_chars < len(body),
        "text": piece,
    }


def t_list_documents(ctx: Ctx, args: dict) -> dict:
    ctx.ensure_fresh()
    where, params = ["1=1"], []
    if args.get("source"):
        where.append("source_id=?")
        params.append(args["source"])
    if args.get("date_from"):
        where.append("date>=?")
        params.append(args["date_from"])
    if args.get("date_to"):
        where.append("date<=?")
        params.append(args["date_to"])
    if args.get("title_contains"):
        where.append("title LIKE ?")
        params.append("%" + str(args["title_contains"]) + "%")
    if args.get("bvid_contains"):
        where.append("bvid LIKE ?")
        params.append("%" + str(args["bvid_contains"]) + "%")
    if not args.get("include_dead"):
        where.append("state='indexed'")
    order = {"date_desc": "date DESC, bvid DESC", "date_asc": "date ASC, bvid ASC",
             "title": "title ASC"}.get(args.get("order", "date_desc"), "date DESC")
    limit = _as_int(args.get("limit"), 50, 1, 200)
    offset = _as_int(args.get("offset"), 0, 0)
    cond = " AND ".join(where)
    total = ctx.conn.execute(f"SELECT COUNT(*) FROM docs WHERE {cond}", params).fetchone()[0]
    rows = []
    for r in ctx.conn.execute(
            f"""SELECT id, source_id, rel_path, title, bvid, date, duration, n_chunks, state, note, size, mtime
                FROM docs WHERE {cond} ORDER BY {order} LIMIT ? OFFSET ?""",
            params + [limit, offset]):
        rows.append({"doc_id": r["id"], "source": r["source_id"], "path": r["rel_path"],
                     "title": r["title"], "bvid": r["bvid"], "date": r["date"],
                     "duration": r["duration"], "chunks": r["n_chunks"], "state": r["state"],
                     "bytes": r["size"], "mtime": r["mtime"], "note": r["note"]})
    return {"total": total, "returned": len(rows), "limit": limit, "offset": offset,
            "ordering": order, "documents": rows, "index": _fresh_brief(ctx)}


def t_stats(ctx: Ctx, args: dict) -> dict:
    conn = ctx.conn
    out = {"counts": store.counts(conn), "sources": [], "model": embed_mod.model_status(ctx.cfg),
           "db_path": ctx.cfg.get("db_path"), "config_path": ctx.cfg.get("_config_path")}
    try:
        out["db_bytes"] = os.path.getsize(ctx.cfg["db_path"])
    except OSError:
        out["db_bytes"] = 0
    for s in store.list_sources(conn):
        row = dict(s)
        row["include"] = json.loads(row.get("include") or "[]")
        row["exclude"] = json.loads(row.get("exclude") or "[]")
        row["head_now"] = indexer.git_head(row["root"])
        out["sources"].append(row)
    last_index = float(store.meta_get(conn, "last_index_ts", 0) or 0)
    out["last_index_ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_index)) if last_index else None
    try:
        diff = indexer.scan_changes(ctx.cfg, conn)
        out["source_files"] = diff["files"]
        out["source_newest_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.localtime(diff["newest_mtime"])) if diff["newest_mtime"] else None
        out["stale"] = diff["stale"]
        out["pending_changes"] = {"new": len(diff["new"]), "changed": len(diff["changed"]),
                                  "removed": len(diff["removed"])}
        out["per_source"] = {k: {"files": v["files"],
                                 "newest": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v["newest_mtime"])) if v["newest_mtime"] else None}
                             for k, v in diff["per_source"].items()}
    except Exception as e:
        out["stale_check_error"] = f"{type(e).__name__}: {e}"
    out["recent_jobs"] = store.recent_jobs(conn, 5)
    dead = [dict(r) for r in conn.execute(
        "SELECT rel_path, bvid, title, note FROM docs WHERE state='dead' ORDER BY rel_path LIMIT 50")]
    out["dead_documents"] = dead
    out["dead_count"] = conn.execute("SELECT COUNT(*) FROM docs WHERE state='dead'").fetchone()[0]
    return out


def t_reindex(ctx: Ctx, args: dict) -> dict:
    full = _as_bool(args.get("full"))          # 严格布尔："false" 必须真的是 False
    source = args.get("source")
    wait = _as_int(args.get("wait_seconds"), 60, 0, 1800)
    cfg = ctx.cfg
    if indexer.indexing_locked(cfg):
        return {"ok": False, "locked": True, "mode": "full" if full else "incremental",
                "runs": [], "message": indexer.LOCK_MESSAGE,
                "hint": "第二步安全开关生效中；用户确认资料修复完毕后把 config.json 的 scan.auto_index 改为 true。"}
    # 已有后台任务在跑时不要并发写同一个索引库（SQLite 单写者）
    if not args.get("force"):
        store.job_reap(ctx.conn)          # 先清掉"进程已死但状态还挂着"的僵尸任务
        known = {s["id"] for s in ctx.cfg.get("sources", [])}
        if source and source not in known:
            return {"ok": False, "error": f"未知 source={source!r}", "known_sources": sorted(known),
                    "hint": "用 stats 或 list_documents 看可用资料库 id；新库要用 kb add-source 添加。"}
        running = [j for j in store.recent_jobs(ctx.conn, 5) if j.get("status") == "running"]
        if running:
            j = running[0]
            return {"ok": False, "busy": True, "mode": "full" if full else "incremental",
                    "running_job": {"id": j["id"], "kind": j["kind"], "done": j["done"],
                                    "total": j["total"], "message": j["message"],
                                    "started_at": j["started_at"]},
                    "message": f"已有任务 {j['id']} 正在运行（{j['done']}/{j['total']}），"
                               "为避免索引库写冲突，本次未执行。",
                    "hint": "等它跑完，或传 force=true 强行排队（会等锁，可能很慢）。"}
    if full and _as_bool(args.get("background"), True):
        jid = store.job_start(ctx.conn, "full-reindex", 0, "排队中（后台进程）")
        script = ROOT / "bin" / "kb_index.py"
        cmd = [sys.executable, str(script), "--full", "--job-id", jid]
        if source:
            cmd += ["--source", source]
        logf = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "kb-mcp" / "logs"
        logf.mkdir(parents=True, exist_ok=True)
        fh = open(logf / f"index-{jid}.log", "a", encoding="utf-8")   # 库外日志
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, cwd=str(ROOT), close_fds=True)
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(cmd, **kwargs)
        return {"mode": "full", "background": True, "job_id": jid,
                "log": str(logf / f"index-{jid}.log"),
                "hint": "全量重建在后台进行（实测约 2.3 小时：5.7 万片段向量化；文本部分约 1 分钟）。"
                        "用 stats 查看 recent_jobs 的 done/total 与 log 文件。"}
    t0 = time.time()
    runs = []
    for s in cfg.get("sources", []):
        if source and s["id"] != source:
            continue
        runs.append(indexer.index_source(ctx.conn, cfg, s, embedder=ctx.embedder, full=full,
                                         budget_seconds=None if not full else None))
        if full and time.time() - t0 > wait:
            break
    return {"mode": "full" if full else "incremental", "background": False,
            "took_ms": int((time.time() - t0) * 1000), "runs": [
                {k: v for k, v in r.items() if k not in ("dead_files", "error_files")} for r in runs],
            "dead_files": [x for r in runs for x in r.get("dead_files", [])][:50],
            "error_files": [x for r in runs for x in r.get("error_files", [])][:50]}


TOOLS = {"search": t_search, "fetch": t_fetch, "list_documents": t_list_documents,
         "stats": t_stats, "reindex": t_reindex}


# ---------- JSON-RPC ----------

def make_response(rid, result=None, error=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    return msg


def handle(ctx: Ctx, req: dict) -> dict | None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        ver = want if want in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return make_response(rid, {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": ctx.cfg.get("server", {}).get("name", "kb-mcp"),
                           "version": __version__},
            "instructions": ("本地转录语料检索服务。用 search 找证据片段，用 fetch 取全文做整理/总结，"
                             "用 list_documents 按日期枚举。数据只读，不会修改资料库。"),
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return make_response(rid, {})
    if method == "logging/setLevel":
        return make_response(rid, {})
    if method == "tools/list":
        return make_response(rid, {"tools": tool_defs()})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = TOOLS.get(name)
        if not fn:
            return make_response(rid, error={"code": -32602, "message": f"未知工具: {name}"})
        t0 = time.time()
        try:
            data = fn(ctx, args)
            if isinstance(data, dict) and data.get("error"):
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return make_response(rid, {"content": [{"type": "text", "text": text}], "isError": True})
            payload = {"tool": name, "ok": True, "took_ms": int((time.time() - t0) * 1000), "data": data}
            return make_response(rid, {"content": [{"type": "text",
                                                   "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                                      "isError": False})
        except Exception as e:
            log(f"工具 {name} 出错: {traceback.format_exc()}")
            text = json.dumps({"tool": name, "ok": False, "error": f"{type(e).__name__}: {e}"},
                              ensure_ascii=False, indent=2)
            return make_response(rid, {"content": [{"type": "text", "text": text}], "isError": True})
    if method in ("resources/list",):
        return make_response(rid, {"resources": []})
    if method in ("prompts/list",):
        return make_response(rid, {"prompts": []})
    if method == "shutdown":
        return make_response(rid, {})
    if method == "exit":
        raise SystemExit(0)
    if rid is None:
        return None   # 未知通知，忽略
    return make_response(rid, error={"code": -32601, "message": f"未实现的方法: {method}"})


def main() -> int:
    global _LOG_FH
    real_out = sys.stdout
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        real_out.reconfigure(encoding="utf-8", newline="\n")       # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    cfg = cfgmod.load()
    lf = cfg.get("server", {}).get("log_file")
    if lf:
        try:
            Path(lf).parent.mkdir(parents=True, exist_ok=True)
            _LOG_FH = open(lf, "a", encoding="utf-8")
        except Exception:
            _LOG_FH = None
    log(f"启动 stdio 服务 pid={os.getpid()} python={sys.version.split()[0]} config={cfg['_config_path']}")

    ctx = None
    try:
        conn = store.connect(cfg["db_path"])
        store.init_schema(conn)
        emb = embed_mod.load_from_config(cfg)
        log(f"索引库={cfg['db_path']} 向量={'可用' if emb.available else '不可用(' + emb.reason + ')'}")
        ctx = Ctx(cfg, conn, emb)
    except Exception:
        log("初始化失败: " + traceback.format_exc())

    sys.stdout = StdoutGuard(real_out)

    def send(obj: dict) -> None:
        real_out.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        real_out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            log("收到非法 JSON: " + line[:200])
            send(make_response(None, error={"code": -32700, "message": "Parse error"}))
            continue
        if isinstance(req, list):
            for one in req:
                try:
                    resp = handle(ctx, one)
                    if resp:
                        send(resp)
                except SystemExit:
                    raise
                except Exception:
                    log("处理批量请求出错: " + traceback.format_exc())
            continue
        try:
            if ctx is None:
                resp = make_response(req.get("id"), error={"code": -32603, "message": "服务初始化失败，请查看 stderr 日志"})
            else:
                resp = handle(ctx, req)
        except SystemExit:
            log("收到 exit，退出")
            return 0
        except Exception:
            log("处理请求出错: " + traceback.format_exc())
            resp = make_response(req.get("id"), error={"code": -32603, "message": "内部错误"})
        if resp:
            send(resp)
    log("stdin 关闭，退出")
    return 0