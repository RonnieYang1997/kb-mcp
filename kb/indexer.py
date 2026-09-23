# -*- coding: utf-8 -*-
"""增量索引器。

只读铁律：对源库只有 os.walk / os.stat / 二进制读取；任何写操作都只发生在索引库（库外）。
证据链：索引前后记录源库 .git/HEAD（纯文件读取，不调用 git），变化即告警。
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import time
from pathlib import Path

from . import store, textproc

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".idea", ".vscode"}


# ---------- 源文件枚举（只读） ----------

def _match(rel: str, name: str, pats: list[str]) -> bool:
    for pat in pats or []:
        p = pat.replace("\\", "/")
        cands = {p}
        if p.startswith("**/"):
            cands.add(p[3:])
        if p.startswith("./"):
            cands.add(p[2:])
        for c in cands:
            if fnmatch.fnmatch(rel, c) or fnmatch.fnmatch(name, c):
                return True
    return False


def iter_files(src: dict) -> list[dict]:
    root = Path(src["root"])
    includes = src.get("include") or ["**/*.md"]
    excludes = list(src.get("exclude") or []) + ["**/.git/**"]
    out: list[dict] = []
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            abs_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(abs_path, root).replace("\\", "/")
            if not _match(rel, fn, includes):
                continue
            if _match(rel, fn, excludes):
                continue
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            out.append({"abs": abs_path, "rel": rel, "name": fn,
                        "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["rel"])
    return out


def git_head(root: str) -> str:
    """纯文件读取取 HEAD 短 sha（绝不调用 git，避免写 .git）。"""
    g = Path(root) / ".git"
    try:
        head = (g / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if head.startswith("ref:"):
        ref = head.split(" ", 1)[1].strip()
        p = g / ref
        try:
            return p.read_text(encoding="utf-8", errors="replace").strip()[:12]
        except OSError:
            pass
        try:
            for line in (g / "packed-refs").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:12]
        except OSError:
            pass
        return ""
    return head[:12]


# ---------- 单文件处理 ----------

def read_bytes(path: str, max_bytes: int) -> bytes:
    with open(path, "rb") as f:          # 只读
        return f.read(max_bytes)


def index_source(conn, cfg, src: dict, embedder=None, full: bool = False,
                 progress=None, job_id: str | None = None,
                 budget_seconds: float | None = None) -> dict:
    t_start = time.time()
    scan = cfg.get("scan", {})
    max_bytes = int(scan.get("max_file_bytes", 8_000_000))
    min_body = int(scan.get("min_body_chars", 30))
    clean = bool(src.get("clean", True))
    existing = store.get_doc_state(conn, src["id"])
    head_before = git_head(src["root"])
    store.upsert_source(conn, src, head=head_before)
    # 索引日志只留 30 天，避免无限增长
    conn.execute("DELETE FROM index_log WHERE ts < ?",
                 (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 30 * 86400)),))
    files = iter_files(src)

    st = {"source": src["id"], "total": len(files), "new": 0, "updated": 0, "unchanged": 0,
          "removed": 0, "dead": 0, "empty": 0, "errors": 0, "chunks": 0, "embedded": 0,
          "head_before": head_before, "head_after": head_before, "budget_hit": False,
          "dead_files": [], "error_files": []}

    seen = set()
    for i, f in enumerate(files):
        seen.add(f["rel"])
        prev = existing.get(f["rel"])
        if (prev and not full and abs((prev["mtime"] or 0) - f["mtime"]) < 1.0
                and (prev["size"] or 0) == f["size"]):
            st["unchanged"] += 1
            if progress and i % 200 == 0:
                progress(i, len(files), st)
            continue
        if budget_seconds and (time.time() - t_start) > budget_seconds:
            st["budget_hit"] = True
            break
        t0 = time.time()
        try:
            raw = read_bytes(f["abs"], max_bytes)
            sha1 = hashlib.sha1(raw).hexdigest()
            if prev and not full and prev["sha1"] == sha1:
                conn.execute("UPDATE docs SET mtime=?, size=? WHERE id=?",
                             (f["mtime"], f["size"], prev["id"]))
                store.log_index(conn, src["id"], f["rel"], "touch", "ok", "内容未变", int((time.time() - t0) * 1000))
                st["unchanged"] += 1
                continue
            text = raw.decode("utf-8", errors="replace")
            meta, body, _raw_fm = textproc.parse_front_matter(text)
            m = textproc.extract_meta(meta, body, f["name"])
            body_clean, cstats = textproc.clean_body(body, m["title"], clean)
            reason = textproc.detect_dead(body_clean, meta)
            chunks: list[str] = []
            note = ""
            if reason:
                state = "dead"
                note = reason
                st["dead"] += 1
                st["dead_files"].append({"rel": f["rel"], "bvid": m["bvid"], "reason": reason})
            elif len(body_clean) < min_body:
                state = "empty"
                note = f"正文仅 {len(body_clean)} 字"
                st["empty"] += 1
            else:
                state = "indexed"
                chunks = textproc.chunk_text(body_clean,
                                             cfg.get("chunk", {}).get("size", 350),
                                             cfg.get("chunk", {}).get("overlap", 80))
            row = {
                "source_id": src["id"], "rel_path": f["rel"], "abs_path": f["abs"],
                "file_name": f["name"], "title": m["title"], "bvid": m["bvid"], "date": m["date"],
                "duration": m["duration"], "vtype": m["vtype"], "status": m["status"],
                "size": f["size"], "mtime": f["mtime"], "sha1": sha1, "n_chunks": len(chunks),
                "state": state, "note": note, "indexed_at": store.now(),
            }
            doc_id = store.upsert_doc(conn, row)
            store.delete_doc_content(conn, doc_id)
            cids = store.insert_chunks(conn, doc_id, chunks)
            st["chunks"] += len(chunks)
            if chunks and embedder is not None and getattr(embedder, "available", False):
                vec = embedder.encode(chunks, prefix=cfg.get("embed", {}).get("doc_prefix", ""))
                store.insert_embeddings(conn, cids, vec, embedder.model_id)
                st["embedded"] += len(cids)
            st["new" if not prev else "updated"] += 1
            store.log_index(conn, src["id"], f["rel"],
                            "add" if not prev else "update", state,
                            f"chunks={len(chunks)} dropped={cstats}", int((time.time() - t0) * 1000))
        except Exception as e:
            st["errors"] += 1
            st["error_files"].append({"rel": f["rel"], "error": f"{type(e).__name__}: {e}"})
            store.log_index(conn, src["id"], f["rel"], "error", "error", f"{type(e).__name__}: {e}",
                            int((time.time() - t0) * 1000))
        if i % 25 == 0:
            conn.commit()
            if progress:
                progress(i, len(files), st)
            if job_id:
                store.job_update(conn, job_id, done=i, total=len(files),
                                 message=f"处理中 {i}/{len(files)}")
    # 已删除的文件
    for rel, prev in existing.items():
        if rel not in seen:
            store.delete_doc(conn, prev["id"])
            store.log_index(conn, src["id"], rel, "delete", "ok", "源文件已不存在")
            st["removed"] += 1

    st["head_after"] = git_head(src["root"])
    st["head_changed"] = bool(st["head_before"] and st["head_after"] != st["head_before"])
    st["took_ms"] = int((time.time() - t_start) * 1000)
    store.meta_set(conn, f"head:{src['id']}", st["head_after"])
    store.meta_set(conn, "last_index_ts", time.time())
    store.meta_set(conn, "last_walk_ts", time.time())
    conn.commit()
    if st["head_changed"]:
        store.log_index(conn, src["id"], "", "warn", "head_changed",
                        f"{st['head_before']} -> {st['head_after']}")
    return st


# ---------- 陈旧检查（惰性兜底） ----------

def _scan_summary(cfg) -> dict:
    info = {"newest_mtime": 0.0, "count": 0, "per_source": {}}
    for src in cfg.get("sources", []):
        files = iter_files(src)
        newest = max((f["mtime"] for f in files), default=0.0)
        info["per_source"][src["id"]] = {"count": len(files), "newest_mtime": newest}
        info["count"] += len(files)
        info["newest_mtime"] = max(info["newest_mtime"], newest)
    return info


def scan_changes(cfg, conn) -> dict:
    """逐文件精确比对（源库 mtime/size vs 索引库），判断是否需要重建。"""
    out = {"files": 0, "new": [], "changed": [], "removed": [], "newest_mtime": 0.0,
           "per_source": {}}
    for src in cfg.get("sources", []):
        files = iter_files(src)
        docs = store.get_doc_state(conn, src["id"])
        seen = set()
        for f in files:
            seen.add(f["rel"])
            prev = docs.get(f["rel"])
            if prev is None:
                out["new"].append({"source": src["id"], "rel": f["rel"]})
            elif abs((prev["mtime"] or 0) - f["mtime"]) > 0.001 or (prev["size"] or 0) != f["size"]:
                out["changed"].append({"source": src["id"], "rel": f["rel"]})
        for rel in docs:
            if rel not in seen:
                out["removed"].append({"source": src["id"], "rel": rel})
        newest = max((f["mtime"] for f in files), default=0.0)
        out["per_source"][src["id"]] = {"files": len(files), "newest_mtime": newest,
                                        "indexed_docs": len(docs)}
        out["files"] += len(files)
        out["newest_mtime"] = max(out["newest_mtime"], newest)
    out["stale"] = bool(out["new"] or out["changed"] or out["removed"])
    return out


def ensure_fresh(conn, cfg, embedder=None, force: bool = False,
                 budget_seconds: float | None = 60.0) -> dict:
    """发现源库比索引新就地重建（增量）。查询路径调用，保证结果不过期。"""
    throttle = float(cfg.get("scan", {}).get("stale_check_seconds", 300))
    last_walk = float(store.meta_get(conn, "last_walk_ts", 0) or 0)
    now = time.time()
    if not force and (now - last_walk) < throttle:
        return {"checked": False, "reason": "throttled",
                "seconds_since_walk": int(now - last_walk),
                "reindexed": False, "throttle_seconds": int(throttle)}
    diff = scan_changes(cfg, conn)
    out = {"checked": True, "reindexed": False, "stale": diff["stale"],
           "source_files": diff["files"], "per_source": diff["per_source"],
           "new": len(diff["new"]), "changed": len(diff["changed"]), "removed": len(diff["removed"]),
           "last_index_ts": float(store.meta_get(conn, "last_index_ts", 0) or 0), "runs": []}
    if force or diff["stale"]:
        for src in cfg.get("sources", []):
            res = index_source(conn, cfg, src, embedder=embedder, full=False,
                               budget_seconds=budget_seconds)
            out["runs"].append(res)
            out["reindexed"] = out["reindexed"] or bool(res["new"] or res["updated"] or res["removed"])
    else:
        store.meta_set(conn, "last_walk_ts", now)
        conn.commit()
    return out