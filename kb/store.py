# -*- coding: utf-8 -*-
"""SQLite 存储层：docs / chunks / chunks_fts(trigram) / embeddings / index_log / jobs。

索引库固定放在库外（默认 %LOCALAPPDATA%\\kb-mcp\\index.db），
本模块只会创建 **索引库所在目录**，永不写入任何源库。
"""
from __future__ import annotations

import json
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
  total INTEGER, done INTEGER, message TEXT);
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
    """返回 (chunk_id 列表, 归一化向量矩阵)。"""
    sql = ("SELECT e.chunk_id AS cid, e.vec AS vec, e.dim AS dim FROM embeddings e "
           "JOIN chunks c ON c.id=e.chunk_id JOIN docs d ON d.id=c.doc_id ")
    if where:
        sql += "WHERE " + where + " "
    sql += "ORDER BY e.chunk_id"
    ids: list[int] = []
    blobs: list[bytes] = []
    dim = None
    for r in conn.execute(sql, params):
        ids.append(r["cid"])
        blobs.append(r["vec"])
        dim = r["dim"] or dim
    if not ids:
        return [], np.zeros((0, dim or 512), dtype=np.float32)
    mat = np.frombuffer(b"".join(blobs), dtype=np.float32)
    mat = mat.reshape(len(blobs), -1)
    return ids, mat


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


def job_start(conn, kind: str, total: int = 0, message: str = "") -> str:
    jid = uuid.uuid4().hex[:12]
    conn.execute("INSERT INTO jobs(id,kind,status,started_at,total,done,message) VALUES(?,?,?,?,?,?,?)",
                 (jid, kind, "running", now(), total, 0, message))
    conn.commit()
    return jid


def job_update(conn, jid: str, done: int | None = None, total: int | None = None,
               message: str | None = None, status: str | None = None) -> None:
    sets, params = [], []
    for col, val in (("done", done), ("total", total), ("message", message), ("status", status)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if not sets:
        return
    params.append(jid)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params)
    conn.commit()


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