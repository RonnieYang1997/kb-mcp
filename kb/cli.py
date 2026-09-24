# -*- coding: utf-8 -*-
"""命令行入口：python -m kb <子命令>"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__, audit, config as cfgmod, embed as embed_mod, indexer, store, textproc


def _out(obj, as_json: bool = False, text: str = "") -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(text or json.dumps(obj, ensure_ascii=False, indent=2))


def _open_store(cfg):
    conn = store.connect(cfg["db_path"])
    store.init_schema(conn)
    return conn


# ---------- 子命令 ----------

def cmd_serve(args) -> int:
    from .server import main
    return main()


def cmd_index(args) -> int:
    cfg = cfgmod.load()
    conn = _open_store(cfg)
    if indexer.indexing_locked(cfg):
        # 第二步安全开关：不读源库、不建索引、不算向量
        if args.job_id:
            store.job_finish(conn, args.job_id, "skipped_locked", indexer.LOCK_MESSAGE)
        print("[kb index] " + indexer.LOCK_MESSAGE, file=sys.stderr)
        if not args.quiet:
            print(json.dumps({"locked": True, "reason": "auto_index=false",
                              "message": indexer.LOCK_MESSAGE,
                              "counts": store.counts(conn)}, ensure_ascii=False, indent=2))
        return 4
    emb = None if getattr(args, "fts_only", False) else embed_mod.load_from_config(cfg)
    if emb is None:
        print("[kb index] --fts-only：本次不向量化（向量可稍后由 reindex 补齐）")
    job_id = getattr(args, "job_id", None)
    if job_id and not store.job_get(conn, job_id):
        n_files = 0
        for src in cfg.get("sources", []):
            if args.source and src["id"] != args.source:
                continue
            try:
                n_files += len(indexer.iter_files(src))
            except Exception:
                pass
        store.job_start(conn, "full" if args.full else "incremental", total=n_files, jid=job_id,
                        message=f"{'全量' if args.full else '增量'}索引开始，共 {n_files} 篇"
                                + ("，含向量化" if emb is not None else "，仅全文"))
    if job_id:
        # 关键：把 pid 登记成"真正干活的那个进程"。后台任务的 pid 不能是派发它的服务进程，
        # 否则进程存活判断会失真（Windows pid 还会被复用）。
        store.job_update(conn, job_id, status="running", pid=os.getpid())
    if not args.quiet:
        print(f"[kb index] full={args.full} source={args.source or '*'}")
        print(f"[kb index] db={cfg['db_path']}")
        print(f"[kb index] embed={'off (--fts-only)' if emb is None else ('ok' if emb.available else 'unavailable: ' + emb.reason)}")
    t0 = time.time()
    runs = []
    for src in cfg.get("sources", []):
        if args.source and src["id"] != args.source:
            continue

        def progress(i, total, st):
            if not args.quiet:
                print(f"  .. {src['id']} {i}/{total} new={st['new']} upd={st['updated']} "
                      f"dead={st['dead']} err={st['errors']}", flush=True)
            if job_id and (i % 25 == 0 or i == total):
                store.job_update(conn, job_id, done=i, total=total, status="running",
                                 message=f"{src['id']} {i}/{total} 新增{st['new']} 更新{st['updated']} "
                                         f"片段{st['chunks']} 向量{st['embedded']}")

        runs.append(indexer.index_source(conn, cfg, src, embedder=emb, full=args.full,
                                         progress=progress, job_id=job_id,
                                         budget_seconds=args.budget or None))
        if job_id:
            _r = runs[-1]
            store.job_update(conn, job_id, done=_r.get("total", 0),
                             message=f"{src['id']} 完成：新增{_r['new']} 更新{_r['updated']} "
                                     f"片段{_r['chunks']} 向量{_r['embedded']}")
    total_ms = int((time.time() - t0) * 1000)
    summary = {"runs": [{k: v for k, v in r.items() if k not in ("dead_files", "error_files")} for r in runs],
               "took_ms": total_ms, "counts": store.counts(conn)}
    if job_id:
        ok = all(not r["errors"] for r in runs)
        store.job_finish(conn, job_id, "done" if ok else "done_with_errors",
                         f"新增 {sum(r['new'] for r in runs)} 更新 {sum(r['updated'] for r in runs)} "
                         f"删除 {sum(r['removed'] for r in runs)} 失效 {sum(r['dead'] for r in runs)} "
                         f"错 {sum(r['errors'] for r in runs)} | {total_ms/1000:.1f}s")
    _out(summary, as_json=True)
    if any(r["head_changed"] for r in runs):
        print("[kb index] ⚠ 源库 HEAD 在索引期间发生变化，请核对", file=sys.stderr)
        return 3
    return 1 if any(r["errors"] for r in runs) else 0


def cmd_search(args) -> int:
    from .search import search as do_search
    cfg = cfgmod.load()
    conn = _open_store(cfg)
    emb = embed_mod.load_from_config(cfg)
    res = do_search(conn, cfg, args.query, embedder=emb, top_k=args.top_k, source=args.source,
                    mode=args.mode, date_from=args.date_from, date_to=args.date_to)
    if args.json:
        _out(res, as_json=True)
        return 0
    print(f"query={res['query']} mode={res['mode']} candidates={res['candidates']} "
          f"took={res['took_ms']}ms notes={res['notes']}")
    for r in res["results"]:
        print(f"\n[{r['rank']}] rrf={r['score']} cosine={r['cosine']} {r['date']} {r['bvid']} "
              f"({r['path']} #{r['seq']})\n    {r['title']}\n    {r['snippet'][:200]}")
    return 0


def cmd_fetch(args) -> int:
    from .server import _doc_text, _resolve_doc
    cfg = cfgmod.load()
    conn = _open_store(cfg)
    doc = _resolve_doc(conn, args.doc_id, args.path, args.bvid)
    if not doc:
        print("未找到文档", file=sys.stderr)
        return 1
    body, m = _doc_text(cfg, doc["source_id"], doc["abs_path"])
    piece = body[args.offset:args.offset + args.max_chars]
    print(json.dumps({"doc_id": doc["id"], "title": doc["title"], "date": doc["date"],
                      "bvid": doc["bvid"], "path": doc["rel_path"],
                      "total_chars": len(body), "returned_chars": len(piece)}, ensure_ascii=False))
    print(piece)
    return 0


def cmd_stats(args) -> int:
    cfg = cfgmod.load()
    conn = _open_store(cfg)
    out = {"counts": store.counts(conn), "sources": store.list_sources(conn),
           "model": embed_mod.model_status(cfg), "db_path": cfg["db_path"],
           "last_index_ts": store.meta_get(conn, "last_index_ts"),
           "recent_jobs": store.recent_jobs(conn, 5)}
    out["indexing_locked"] = indexer.indexing_locked(cfg)
    if out["indexing_locked"]:
        out["note"] = indexer.LOCK_MESSAGE
    else:
        try:
            diff = indexer.scan_changes(cfg, conn)
            out["source_files"] = diff["files"]
            out["per_source"] = diff["per_source"]
            out["stale"] = diff["stale"]
            out["pending_changes"] = {"new": len(diff["new"]), "changed": len(diff["changed"]),
                                      "removed": len(diff["removed"])}
        except Exception as e:
            out["scan_error"] = f"{type(e).__name__}: {e}"
    _out(out, as_json=True)
    return 0


def cmd_sources(args) -> int:
    cfg = cfgmod.load()
    for s in cfg.get("sources", []):
        files = indexer.iter_files(s)
        print(f"- {s['id']}  [{s.get('label')}]\n    root    = {s['root']}\n"
              f"    files   = {len(files)}\n    include = {s.get('include')}\n"
              f"    read_only={s.get('read_only')} clean={s.get('clean')}")
    return 0


def cmd_add_source(args) -> int:
    cfg = cfgmod.load()
    root = str(Path(args.root).resolve())
    if any(s["root"] == root for s in cfg.get("sources", [])):
        print("该资料库已存在", file=sys.stderr)
        return 1
    cfg.setdefault("sources", []).append({
        "id": args.id or Path(root).name, "label": args.label or (args.id or Path(root).name),
        "root": root, "include": args.include or ["**/*.md"], "exclude": ["**/.git/**"],
        "read_only": True, "clean": True})
    cfgmod.save(cfg)
    print(f"已写入 {cfgmod.config_path()}；新增库 id={args.id or Path(root).name}")
    print("下一步：kb index  →  即完成该库的索引")
    return 0


def cmd_verify(args) -> int:
    """只读体检：统计库里各类缺陷残留（不建索引、不写任何文件）。"""
    cfg = cfgmod.load()
    report = {"sources": [], "generated_at": store.now()}
    for src in cfg.get("sources", []):
        files = indexer.iter_files(src)
        st = {"id": src["id"], "root": src["root"], "files": len(files), "bom": 0, "gbk_lines": 0,
              "dead": 0, "no_front_matter": 0, "too_short": 0, "dead_list": [],
              "newest_mtime": None, "oldest_mtime": None, "total_chars": 0}
        mtimes = []
        for f in files:
            with open(f["abs"], "rb") as fh:      # 只读
                raw = fh.read()
            mtimes.append(f["mtime"])
            if raw.startswith(b"\xef\xbb\xbf"):
                st["bom"] += 1
            text = raw.decode("utf-8", errors="replace")
            meta, body, _ = textproc.parse_front_matter(text)
            if not meta:
                st["no_front_matter"] += 1
            junk = textproc.junk_scan(text)
            st["gbk_lines"] += junk["pua_lines"] + junk["marker_lines"]
            m = textproc.extract_meta(meta, body, f["name"])
            clean, _ = textproc.clean_body(body, m["title"], True)
            st["total_chars"] += len(clean)
            if textproc.detect_dead(clean, meta):
                st["dead"] += 1
                st["dead_list"].append({"path": f["rel"], "bvid": m["bvid"],
                                        "title": m["title"], "chars": len(clean)})
            elif len(clean) < 200:
                st["too_short"] += 1
        if mtimes:
            st["newest_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(max(mtimes)))
            st["oldest_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(min(mtimes)))
        report["sources"].append(st)
    _out(report, as_json=True)
    problems = sum(s["bom"] + s["gbk_lines"] + s["dead"] + s["no_front_matter"] for s in report["sources"])
    print(f"\n[体检] 需要关注的项合计 = {problems}", file=sys.stderr)
    return 1 if problems else 0


def cmd_doctor(args) -> int:
    cfg = cfgmod.load()
    conn = _open_store(cfg)
    info = {
        "version": __version__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "config_path": cfg["_config_path"],
        "config_exists": Path(cfg["_config_path"]).exists(),
        "db_path": cfg["db_path"],
        "db_exists": Path(cfg["db_path"]).exists(),
        "counts": store.counts(conn),
        "model": embed_mod.model_status(cfg),
        "audit": audit.summarize(),
    }
    emb = embed_mod.load_from_config(cfg)
    info["model"]["loadable"] = emb.available
    info["model"]["reason"] = emb.reason
    # 源库 HEAD 快照（纯文件读取）
    info["indexing_locked"] = indexer.indexing_locked(cfg)
    info["sources"] = []
    for s in cfg.get("sources", []):
        files = None if info["indexing_locked"] else indexer.iter_files(s)
        info["sources"].append({
            "id": s["id"], "root": s["root"],
            "files": (len(files) if files is not None else None),
            "head": indexer.git_head(s["root"]),
            "head_at_index": store.meta_get(conn, f"head:{s['id']}", ""),
            "read_only": s.get("read_only", True), "clean": s.get("clean", True),
        })
    _out(info, as_json=True)
    print("\n[只读自审] " + ("干净：无未解释的写操作" if info["audit"]["clean"]
                          else f"发现 {len(info['audit']['unexplained'])} 处未解释的写操作，请核对"),
          file=sys.stderr)
    if info["indexing_locked"]:
        print("[索引开关] " + indexer.LOCK_MESSAGE, file=sys.stderr)
    else:
        print("[索引开关] auto_index=true —— 已允许读取源库并建立索引", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="kb", description=f"kb-mcp {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="以 MCP stdio 方式运行（供 AionUi 调用）")

    p = sub.add_parser("index", help="增量/全量建索引")
    p.add_argument("--full", action="store_true")
    p.add_argument("--source")
    p.add_argument("--job-id")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--fts-only", action="store_true",
                   help="只建全文索引（约 1 分钟），向量留待后台补，先让检索可用")
    p.add_argument("--budget", type=float, default=0.0, help="最长运行秒数，0=不限")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("search", help="混合检索")
    p.add_argument("query")
    p.add_argument("--top-k", type=int)
    p.add_argument("--mode", default="hybrid", choices=["hybrid", "fts", "vector"])
    p.add_argument("--source")
    p.add_argument("--date-from")
    p.add_argument("--date-to")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("fetch", help="取一篇全文")
    p.add_argument("--doc-id", type=int)
    p.add_argument("--path")
    p.add_argument("--bvid")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=20000)
    p.set_defaults(fn=cmd_fetch)

    sub.add_parser("stats", help="索引状态").set_defaults(fn=cmd_stats)
    sub.add_parser("sources", help="列出资料库").set_defaults(fn=cmd_sources)
    sub.add_parser("verify", help="只读体检：统计缺陷残留").set_defaults(fn=cmd_verify)
    sub.add_parser("doctor", help="环境与只读自审").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("add-source", help="增加资料库（口子）")
    p.add_argument("--root", required=True)
    p.add_argument("--id")
    p.add_argument("--label")
    p.add_argument("--include", action="append")
    p.set_defaults(fn=cmd_add_source)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())