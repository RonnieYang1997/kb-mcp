# -*- coding: utf-8 -*-
"""SQLite 存储层：docs / chunks / chunks_fts(trigram) / embeddings / index_log / jobs。

索引库固定放在库外（默认 %LOCALAPPDATA%\\kb-mcp\\index.db），
本模块只会创建 **索引库所在目录**，永不写入任何源库。
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS sources(
  id TEXT PRIMARY KEY, label TEXT, root TEXT, include TEXT, exclude TEXT,
  read_only INTEGER DEFAULT 1, clean INTEGER DEFAULT 1, head TEXT, added_at TEXT);

CREATE TABLE IF NOT EXISTS docs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL, rel_path TEXT NOT NULL, abs_path TEXT NOT NULL,
  file_name TEXT, title TEXT, bvid TEXT, date TEXT, duration TEXT, vtype TEXT,
  status TEXT, size INTEGER, mtime REAL, sha1 TEXT, n_chunks INTEGER DEFAULT 0,
  state TEXT, note TEXT, indexed_at TEXT,
  UNIQUE(source_id, rel_path));
CREATE INDEX IF NOT EXISTS idx_docs_date ON docs(date);
CREATE INDEX IF NOT EXISTS idx_docs_bvid ON docs(bvid);
CREATE INDEX IF NOT EXISTS idx_docs_src  ON docs(source_id);

CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id INTEGER NOT NULL, seq INTEGER, text TEXT, n_chars INTEGER);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='trigram');

CREATE TABLE IF NOT EXISTS embeddings(
  chunk_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB, model TEXT);

CREATE TABLE IF NOT EXISTS index_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source_id TEXT, rel_path TEXT,
  action TEXT, result TEXT, note TEXT, ms INTEGER);
CREATE INDEX IF NOT EXISTS idx_log_ts ON index_log(ts);

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT, status TEXT, started_at TEXT, finished_at TEXT,
  total INTEGER, done INTEGER, message TEXT, pid INTEGER DEFAULT 0, updated_at TEXT DEFAULT '');
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)  # 只建索引库目录（库外）
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 老库平滑迁移：jobs 表补 pid / updated_at（用于判断后台任务是否还活着）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "pid" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN pid INTEGER DEFAULT 0")
    if "updated_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN updated_at TEXT DEFAULT ''")
    conn.commit()


# ---------- meta ----------

def meta_get(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, "" if value is None else str(value)))


# ---------- sources ----------

def upsert_source(conn, src: dict, head: str = "") -> None:
    conn.execute(
        """INSERT INTO sources(id,label,root,include,exclude,read_only,clean,head,added_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET label=excluded.label, root=excluded.root,
             include=excluded.include, exclude=excluded.exclude,
             read_only=excluded.read_only, clean=excluded.clean""",
        (src["id"], src.get("label", src["id"]), src["root"],
         json.dumps(src.get("include", []), ensure_ascii=False),
         json.dumps(src.get("exclude", []), ensure_ascii=False),
         1 if src.get("read_only", True) else 0,
         1 if src.get("clean", True) else 0, head, now()))
    conn.commit()


def list_sources(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id")]


# ---------- docs / chunks ----------

def get_doc_state(conn, source_id: str) -> dict[str, dict]:
    out = {}
    for r in conn.execute(
            "SELECT id, rel_path, size, mtime, sha1, state, n_chunks FROM docs WHERE source_id=?",
            (source_id,)):
        out[r["rel_path"]] = dict(r)
    return out


def upsert_doc(conn, row: dict) -> int:
    conn.execute(
        """INSERT INTO docs(source_id,rel_path,abs_path,file_name,title,bvid,date,duration,
                            vtype,status,size,mtime,sha1,n_chunks,state,note,indexed_at)
           VALUES(:source_id,:rel_path,:abs_path,:file_name,:title,:bvid,:date,:duration,
                  :vtype,:status,:size,:mtime,:sha1,:n_chunks,:state,:note,:indexed_at)
           ON CONFLICT(source_id,rel_path) DO UPDATE SET
             abs_path=excluded.abs_path, file_name=excluded.file_name, title=excluded.title,
             bvid=excluded.bvid, date=excluded.date, duration=excluded.duration,
             vtype=excluded.vtype, status=excluded.status, size=excluded.size,
             mtime=excluded.mtime, sha1=excluded.sha1, n_chunks=excluded.n_chunks,
             state=excluded.state, note=excluded.note, indexed_at=excluded.indexed_at""",
        row)
    r = conn.execute("SELECT id FROM docs WHERE source_id=? AND rel_path=?",
                     (row["source_id"], row["rel_path"])).fetchone()
    return r["id"]


def delete_doc_content(conn, doc_id: int) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
    conn.execute("DELETE FROM embeddings WHERE chunk_id IN (SELECT id FROM chunks WHERE doc_id=?)", (doc_id,))
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def delete_doc(conn, doc_id: int) -> None:
    delete_doc_content(conn, doc_id)
    conn.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def insert_chunks(conn, doc_id: int, chunks: list[str]) -> list[int]:
    ids = []
    for seq, text in enumerate(chunks):
        cur = conn.execute("INSERT INTO chunks(doc_id,seq,text,n_chars) VALUES(?,?,?,?)",
                           (doc_id, seq, text, len(text)))
        cid = cur.lastrowid
        conn.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cid, text))
        ids.append(cid)
    return ids


def insert_embeddings(conn, chunk_ids: list[int], mat: np.ndarray, model: str) -> None:
    rows = [(int(cid), int(mat.shape[1]), mat[i].astype(np.float32).tobytes(), model)
            for i, cid in enumerate(chunk_ids)]
    conn.executemany(
        "INSERT INTO embeddings(chunk_id,dim,vec,model) VALUES(?,?,?,?) "
        "ON CONFLICT(chunk_id) DO UPDATE SET dim=excluded.dim, vec=excluded.vec, model=excluded.model",
        rows)


def load_vectors(conn, where: str = "", params: tuple = ()) -> tuple[list[int], np.ndarray]:
    """返回 (chunk_id 列表, 归一化向量矩阵)。

    只取"占多数"的那一个模型+维度：索引库里一旦混进两种向量（换模型后没重建），
    按 blob 长度盲目 reshape 会静默算出错误的相似度，宁可少取也不能算错。
    """
    dominant = conn.execute(
        "SELECT model, dim FROM embeddings GROUP BY model, dim "
        "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    if dominant is None:
        return [], np.zeros((0, 512), dtype=np.float32)
    sql = ("SELECT e.chunk_id AS cid, e.vec AS vec FROM embeddings e "
           "JOIN chunks c ON c.id=e.chunk_id JOIN docs d ON d.id=c.doc_id "
           "WHERE e.model = ? AND e.dim = ? ")
    args = [dominant["model"], dominant["dim"]]
    if where:
        sql += "AND " + where + " "
    sql += "ORDER BY e.chunk_id"
    ids: list[int] = []
    blobs: list[bytes] = []
    for r in conn.execute(sql, args + list(params)):
        ids.append(r["cid"])
        blobs.append(r["vec"])
    if not ids:
        return [], np.zeros((0, int(dominant["dim"]) or 512), dtype=np.float32)
    size = len(blobs[0])
    if size % 4 or any(len(b) != size for b in blobs):
        raise RuntimeError(f"向量 blob 长度不一致（首个 {size} 字节），索引库可能损坏或被混入不同维度")
    mat = np.frombuffer(b"".join(blobs), dtype=np.float32)
    mat = mat.reshape(len(blobs), size // 4)
    return ids, mat


def embeddings_model_info(conn) -> dict:
    """索引库里现存的向量模型分布（用于 stats / 一致性告警）。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT model, dim, COUNT(*) AS n FROM embeddings GROUP BY model, dim ORDER BY n DESC")]
    return {"distinct": len(rows), "groups": rows}


def doc_of_chunk(conn, chunk_ids: list[int]) -> dict[int, dict]:
    if not chunk_ids:
        return {}
    qs = ",".join("?" * len(chunk_ids))
    out = {}
    for r in conn.execute(
            f"""SELECT c.id AS cid, c.seq AS seq, c.n_chars AS n_chars, d.id AS doc_id,
                       d.title AS title, d.bvid AS bvid, d.date AS date, d.rel_path AS rel_path,
                       d.source_id AS source_id, c.text AS text
                FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE c.id IN ({qs})""", chunk_ids):
        out[r["cid"]] = dict(r)
    return out


# ---------- logging / jobs ----------

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log_index(conn, source_id: str, rel_path: str, action: str, result: str,
              note: str = "", ms: int = 0) -> None:
    conn.execute("INSERT INTO index_log(ts,source_id,rel_path,action,result,note,ms) VALUES(?,?,?,?,?,?,?)",
                 (now(), source_id, rel_path, action, result, note, ms))


def job_start(conn, kind: str, total: int = 0, message: str = "", jid: str = "") -> str:
    """建一条 running 任务。传 jid 时用调用方给的 id（后台全量任务用固定 id 便于查询）。"""
    jid = jid or uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO jobs(id,kind,status,started_at,total,done,message,pid,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (jid, kind, "running", now(), total, 0, message, os.getpid(), now()))
    conn.commit()
    return jid


def job_update(conn, jid: str, done: int | None = None, total: int | None = None,
               message: str | None = None, status: str | None = None,
               pid: int | None = None) -> None:
    sets, params = [], []
    for col, val in (("done", done), ("total", total), ("message", message),
                     ("status", status), ("pid", pid)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(now())
    params.append(jid)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def job_reap(conn, max_age_seconds: int = 30 * 60, max_silence_seconds: int = 15 * 60) -> list[str]:
    """把"进程已经没了 / 早就不再心跳"的 running 任务标记为 interrupted。

    否则一个被强杀的后台任务会永远挡住后续索引（写锁保护会一直以为有任务在跑）。
    判断依据两条，缺一不可：
      · 进程还活着（pid 由真正干活的子进程自己登记；后台任务会重新登记为自己的 pid）
      · 并且 15 分钟内有心跳（progress 每 25 篇更新一次）
    只看 pid 会被 Windows 的 pid 复用骗到；只看时间会误杀慢任务。
    """
    killed = []
    for r in conn.execute("SELECT id, pid, started_at, updated_at FROM jobs WHERE status='running'"):
        pid = r["pid"] or 0
        try:
            silence = time.time() - datetime.datetime.fromisoformat(
                r["updated_at"] or r["started_at"]).timestamp()
        except (TypeError, ValueError):
            silence = max_age_seconds + 1
        if pid and _pid_alive(pid) and silence < max_silence_seconds:
            continue
        if not pid and silence < max_age_seconds:
            continue
        conn.execute("UPDATE jobs SET status='interrupted', finished_at=?, "
                     "message=COALESCE(message,'')||' [进程已不在或超过15分钟没心跳，标记为中断]' "
                     "WHERE id=?", (now(), r["id"]))
        killed.append(r["id"])
    if killed:
        conn.commit()
    return killed


def job_finish(conn, jid: str, status: str = "done", message: str = "") -> None:
    conn.execute("UPDATE jobs SET status=?, finished_at=?, message=? WHERE id=?",
                 (status, now(), message, jid))
    conn.commit()


def job_get(conn, jid: str) -> dict | None:
    r = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(r) if r else None


def recent_jobs(conn, limit: int = 5) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,))]


# ---------- stats ----------

def counts(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    out = {
        "docs": q("SELECT COUNT(*) FROM docs"),
        "docs_indexed": q("SELECT COUNT(*) FROM docs WHERE state='indexed'"),
        "docs_dead": q("SELECT COUNT(*) FROM docs WHERE state='dead'"),
        "docs_empty": q("SELECT COUNT(*) FROM docs WHERE state='empty'"),
        "chunks": q("SELECT COUNT(*) FROM chunks"),
        "embeddings": q("SELECT COUNT(*) FROM embeddings"),
        "fts_rows": q("SELECT COUNT(*) FROM chunks_fts"),
    }
    return out