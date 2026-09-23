# -*- coding: utf-8 -*-
"""纯内存文本处理：frontmatter 解析、只读清洗、错误正文识别、切块。

铁律：本模块只有 str/dict/list 进出，永不打开文件、永不写盘。
"""
from __future__ import annotations

import re

# 私用区字符（GBK 误码产物）：U+E000–U+F8FF
RE_PUA = re.compile("[\ue000-\uf8ff]")
# GBK 误码常见残留：锛 / 鏃ユ湡（"日期"被解成 GBK 的产物）
JUNK_MARKERS = ("锛", "鏃ユ湡", "璇煶杞綍")
RE_JUNK_DATE = re.compile(r"^\s*@\{page=.*?\}\s*\.?\s*date\)?\s*$")
RE_JUNK_BVID = re.compile(r"^\s*BVID\b[^\n]{0,4}bvid\s*$", re.I)
RE_FM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_\-]*)\s*:\s*(.*)$")
RE_ATX = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
RE_DEAD_MARK = re.compile(
    r"upstream_error|invalid_request_error|Request Entity Too Large|"
    r"Bad Gateway|Gateway Time-?out|Service Unavailable|Internal Server Error",
    re.I,
)
RE_HTML = re.compile(r"<html|<!doctype", re.I)
RE_FENCE = re.compile(r"^---\s*$")


def strip_bom(text: str) -> str:
    return text.lstrip("\ufeff")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'“”‘’":
        v = v[1:-1]
    return v.strip()


def parse_front_matter(text: str) -> tuple[dict, str, str]:
    """返回 (meta, body, raw_front_matter)。没有 frontmatter 时 meta 为空 dict。"""
    t = normalize_newlines(strip_bom(text))
    lines = t.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, t, ""
    end = -1
    for i in range(1, len(lines)):
        if RE_FENCE.match(lines[i]):
            end = i
            break
    if end < 0:
        return {}, t, ""
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        m = RE_FM_KEY.match(line)
        if m:
            meta[m.group(1).lower()] = _unquote(m.group(2))
    return meta, "\n".join(lines[end + 1:]), "\n".join(lines[1:end])


def is_junk_line(line: str, title: str = "") -> str | None:
    """返回丢弃原因，或 None 表示保留。"""
    stripped = line.strip()
    if not stripped:
        return None
    if RE_PUA.search(line) or any(mk in line for mk in JUNK_MARKERS):
        return "gbk_mojibake"
    if RE_JUNK_DATE.match(line):
        return "junk_header_date"
    if RE_JUNK_BVID.match(line):
        return "junk_header_bvid"
    m = RE_ATX.match(line)
    if m and title and m.group(1).strip() == title.strip():
        return "duplicate_title_heading"
    return None


def clean_body(body: str, title: str = "", enabled: bool = True) -> tuple[str, dict]:
    """只读清洗：剥 BOM、丢乱码行、压缩空行。返回 (清洗后正文, 统计)。"""
    text = normalize_newlines(strip_bom(body))
    stats = {"dropped_gbk": 0, "dropped_junk_hdr": 0, "dropped_dup_heading": 0}
    if not enabled:
        return text.strip(), stats
    kept: list[str] = []
    for line in text.split("\n"):
        reason = is_junk_line(line, title)
        if reason == "gbk_mojibake":
            stats["dropped_gbk"] += 1
            continue
        if reason in ("junk_header_date", "junk_header_bvid"):
            stats["dropped_junk_hdr"] += 1
            continue
        if reason == "duplicate_title_heading":
            stats["dropped_dup_heading"] += 1
            continue
        kept.append(line.rstrip())
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip(), stats


def detect_dead(body: str, meta: dict | None = None) -> str | None:
    """识别「正文失效」：只含错误体。返回原因或 None。"""
    b = body.strip()
    if not b:
        return "empty_body"
    head = b[:1500]
    if RE_DEAD_MARK.search(head):
        return "api_error_body"
    if len(b) < 4000 and RE_HTML.search(b[:400]):
        return "html_error_body"
    if b.startswith('{"error"') or b.startswith("{\n  \"error\"") or b.startswith("{'error'"):
        return "json_error_body"
    if meta and str(meta.get("status", "")).lower() in ("dead", "error", "failed"):
        return "status_marked_dead"
    return None


def filename_bvid(file_name: str) -> str:
    stem = file_name[:-3] if file_name.lower().endswith(".md") else file_name
    if stem.lower().startswith("dufu-"):
        return stem[5:]
    return ""


def extract_meta(meta: dict, body: str, file_name: str) -> dict:
    """把两代 frontmatter 归一化成统一字段。"""
    title = (meta.get("title") or "").strip()
    desc = (meta.get("description") or "").strip()
    if not title and desc:
        title = re.sub(r"\s*[（(]\d{4}-\d{2}-\d{2}[)）]\s*$", "", desc).strip()
    if not title:
        for line in body.split("\n"):
            m = RE_ATX.match(line)
            if m and m.group(1).strip():
                title = m.group(1).strip()
                break
    if not title:
        name = (meta.get("name") or "").strip()
        title = name or filename_bvid(file_name) or file_name

    bvid = (meta.get("bvid") or "").strip() or filename_bvid(file_name)
    date = (meta.get("date") or "").strip()
    if not date and desc:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", desc)
        if m:
            date = m.group(1)
    return {
        "title": title[:300],
        "bvid": bvid,
        "date": date,
        "duration": (meta.get("duration") or "").strip(),
        "vtype": (meta.get("type") or "").strip(),
        "status": (meta.get("status") or "").strip(),
        "source": (meta.get("source") or "").strip(),
        "transcribed": (meta.get("transcribed") or "").strip(),
    }


def chunk_text(text: str, size: int = 350, overlap: int = 80, min_chars: int = 20) -> list[str]:
    """固定窗口切块；尽量在换行处收尾，避免把词切两半。"""
    t = normalize_newlines(text)
    n = len(t)
    if n == 0:
        return []
    if n <= size:
        s = t.strip()
        return [s] if len(s) >= min_chars else ([s] if s else [])
    step = max(1, size - overlap)
    chunks: list[str] = []
    start = 0
    while start < n:
        end = min(n, start + size)
        if end < n:
            window_start = max(start + 1, end - 60)
            cut = t.rfind("\n", window_start, end)
            if cut > start:
                end = cut
        piece = t[start:end].strip()
        if len(piece) >= min_chars or not chunks:
            chunks.append(piece)
        if end >= n:
            break
        nxt = end - overlap
        if nxt <= start:          # 保底推进，避免死循环
            nxt = start + step
        start = nxt
    # 去重（相邻重复块）
    out: list[str] = []
    for c in chunks:
        if not out or out[-1] != c:
            out.append(c)
    return [c for c in out if c]


def snippet(text: str, max_chars: int = 240) -> str:
    t = re.sub(r"\s+", " ", normalize_newlines(text)).strip()
    return t if len(t) <= max_chars else t[:max_chars] + "…"


def junk_scan(text: str) -> dict:
    """统计清洗后仍残留的可疑行（供 doctor / 体检报告）。"""
    pua = 0
    marker = 0
    for line in normalize_newlines(strip_bom(text)).split("\n"):
        if RE_PUA.search(line):
            pua += 1
        if any(mk in line for mk in JUNK_MARKERS):
            marker += 1
    return {"pua_lines": pua, "marker_lines": marker}