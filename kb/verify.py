# -*- coding: utf-8 -*-
"""引文核对：把「我要引用的这句话」拿回只读原文里逐字对一遍。

为什么必须回原文，而不是只查索引库
--------------------------------
1. 索引里的 ``chunks.text`` 是**清洗过**的（去 BOM、丢乱码表头/小标题）。
   于是"原文里有、索引里没有"的句子会被误判成"查不到"。
2. 索引里的正文是**切块过**的（350 字窗口 / 80 重叠）。
   一句跨了片段边界的话，在任何一个片段里都不完整——只查 ``chunks`` 一定给假阴性。

所以本模块的分工是：**用 FTS 快速定位候选（便宜），判定一律回到源文件正文（准）**。
返回里专门带一个 ``in_chunk`` 标志，用来暴露上面第 2 种情况。

对外入口
--------
``load_source_text(cfg, source_id, abs_path)``     只读读取 + 同款清洗，返回 (正文, meta, 原始文本)
``split_ellipsis(quote)``                          把带省略号的引文拆成可核对的片段
``verify_quote(conn, cfg, quote, ...)``            核对单条引文
``verify_quotes(conn, cfg, quotes, ...)``          批量核对（MCP 工具用它）
``extract_quotes(md_text)``                        从 Markdown 文稿里抽出引文并关联 BVID
``verify_article(conn, cfg, md_text, ...)``        整篇文稿核对（CLI ``kb verify-article`` 用它）

判定档位（``status``，越靠前越干净）
----------------------------------
``verbatim``     逐字一致 —— 只有这一档才算"可以直接引用"
``whitespace``   只差空白/全半角（转录把换行压掉很常见），文字一字不差
``loose``        只差标点（转录本身标点就不统一），文字一字不差
``annotated``    文字一字不差，但引文里插了原文没有的括注
``modified``     引文与原文有**实质**差异：多字、少字、改字（含"你标注这篇里有一句几乎
                 一模一样的，但你不是照它抄的"——这时 matched_text 就是该照抄的原文）
``other_doc``    引文是真的，但不在你标注的那一篇里 —— 出处标错了
``not_in_body``  正文里没有，但原始文件里有 —— 通常在乱码表头/元数据区，不该引
``not_found``    库里根本没有这句话（含模糊建议，告诉你原文大概是怎么写的）
``error``        引文为空/太短、BVID 或 doc_id 不存在等
"""
from __future__ import annotations

import difflib
import os
import re
import unicodedata

from . import indexer, search as search_mod, textproc

# 引文去掉空白/标点后至少要有这么多字才值得核对（避免拿"啊""嗯"去查全库）
MIN_QUOTE_CHARS = 4
# 未指定出处时，最多回原文核对多少篇候选
MAX_CANDIDATE_DOCS = 12
MAX_SUGGESTIONS = 3
MAX_DIFF_ITEMS = 6
# 给了出处、而出处里恰好有一句「几乎一模一样」的 → 判 modified（引文有实质差异），
# 而不是 not_found（其实那段就在你标注这篇里，只是你写的和原文不一样）。
# 0.6 是实测出来的分界：改一两个字的引文相似度 ≥0.68，自撰的句子最高只到 0.19。
MODIFIED_RATIO = 0.6
# 相似度低于这个数的「建议」只是噪声（碰巧共用几个词），不如明说「没找到相近的句子」
SUGGEST_MIN_RATIO = 0.3
# 省略号的各种写法（含「……」「...」「<...>」「【…】」）
ELLIPSIS_RE = re.compile(r"(?:…+|\.{3,}|【\s*…+\s*】|\[\s*…+\s*\]|<\s*…+\s*>)")
# 引文两端可能出现、但不属于原文的引号/括号/装饰符
EDGE_CHARS = " \t\r\n\u3000「」『』“”\"'‘’`（）()[]【】《》<>·*——　"
# Markdown 强调符（引文里被加粗标出来的部分要去掉才能对上原文）
MD_MARKS = re.compile(r"(\*\*|__|\*|_|`)")
# 引文里作者自己插的括注（例：「他去了（其实是被人骗去）那个地方」）——
# 这不是原文，但属于常见的引用体例，单独识别出来而不是判成"查不到"。
BRACKET_RE = re.compile(r"[（(][^（()）]{1,40}[)）]")
BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

_DOC_COLS_D = ("d.id, d.source_id, d.rel_path, d.abs_path, d.file_name, d.title, "
               "d.bvid, d.date, d.n_chunks, d.state, d.status")

_SEV = {"verbatim": 0, "whitespace": 1, "annotated": 2, "loose": 3, "modified": 4,
        "other_doc": 5, "not_in_body": 6, "not_found": 7, "error": 8}
_VERDICT = {
    "verbatim": "逐字一致，可放心引用",
    "whitespace": "仅空白/全半角差异，文字一字不差",
    "annotated": "文字一字不差，但引文里插了原文没有的括注",
    "loose": "仅标点差异（转录标点本身不统一），文字一字不差",
    "modified": "引文与原文有实质差异（多字/少字/改字），必须改",
    "other_doc": "引文是真的，但不在你标注的那一篇里——出处标错了",
    "not_in_body": "正文里没有这句；原始文件里才有（可能在乱码表头或元数据区），不要引用",
    "not_found": "库里查不到这句话",
    "error": "引文无效或出处不存在",
}


# ---------------------------------------------------------------- 归一化

def _nfkc1(ch: str) -> str:
    """单字符 NFKC。只在长度不变时采用，保证"归一化视图"的偏移可映射回原文。"""
    n = unicodedata.normalize("NFKC", ch)
    return n if len(n) == 1 else ch


def _tidy(s: str) -> str:
    return (s or "").strip(EDGE_CHARS)


def _clean_quote(s: str) -> str:
    """去掉 Markdown 强调符与两端装饰符，得到"准备核对"的引文。"""
    return _tidy(MD_MARKS.sub("", s or ""))


def _keep_tight(ch: str) -> bool:
    return not _nfkc1(ch).isspace()


def _keep_loose(ch: str) -> bool:
    c = _nfkc1(ch)
    if c.isspace():
        return False
    return unicodedata.category(c)[0] not in ("P", "Z", "C")


def _tight(s: str) -> str:
    return "".join(_nfkc1(c) for c in (s or "") if _keep_tight(c))


def _loose(s: str) -> str:
    return "".join(_nfkc1(c) for c in (s or "") if _keep_loose(c))


def _has_content(s: str) -> bool:
    """是否含"实质内容"（去掉空白与标点后还有东西）。"""
    return bool(_loose(s))


def split_ellipsis(quote: str) -> tuple[list[str], bool]:
    """把带省略号的引文拆成若干片段（每段单独核对）。返回 (片段, 是否原本含省略号)。"""
    raw = quote or ""
    had = bool(ELLIPSIS_RE.search(raw))
    parts = [_clean_quote(p) for p in ELLIPSIS_RE.split(raw)]
    parts = [p for p in parts if _has_content(p)]
    return (parts or [_clean_quote(raw)]), had


class _Hay:
    """一段原文，外加两个"归一化视图"。视图里的字符仍按原文顺序排列，
    因此可以借映射表把命中位置还原成原文偏移。"""

    def __init__(self, text: str):
        self.text = text or ""
        tc, tm, lc, lm = [], [], [], []
        for i, ch in enumerate(self.text):
            if _keep_tight(ch):
                tc.append(_nfkc1(ch)); tm.append(i)
            if _keep_loose(ch):
                lc.append(_nfkc1(ch)); lm.append(i)
        self.tight, self.tight_map = "".join(tc), tm
        self.loose, self.loose_map = "".join(lc), lm

    def locate(self, quote: str) -> dict | None:
        """定位引文。依次尝试 逐字 → 忽略空白/全半角 → 忽略标点。"""
        q = _clean_quote(quote)
        if not q:
            return None
        pos = self.text.find(q)
        if pos >= 0:
            return {"tier": "exact", "start": pos, "end": pos + len(q),
                    "count": self.text.count(q)}
        out = self._locate_in(self.tight, self.tight_map, _tight(q))
        if out:
            out["tier"] = "whitespace"
            out["count"] = self.tight.count(_tight(q))
            return out
        out = self._locate_in(self.loose, self.loose_map, _loose(q))
        if out:
            out["tier"] = "loose"
            out["count"] = self.loose.count(_loose(q))
            return out
        return None

    @staticmethod
    def _locate_in(view: str, vmap: list[int], needle: str) -> dict | None:
        if not needle:
            return None
        pos = view.find(needle)
        if pos < 0:
            return None
        return {"start": vmap[pos], "end": vmap[pos + len(needle) - 1] + 1}


def _diff_detail(quote: str, actual: str) -> tuple[list[str], list[str]]:
    """引文 vs 原文实际文字：返回 (引文里多出来的, 原文里有而引文缺的)。
    只报"实质内容"的差异——纯空白、纯标点的出入不算（那由档位表达）。"""
    q = _clean_quote(quote)
    sm = difflib.SequenceMatcher(None, q, actual, autojunk=False)
    extra, missing = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        a, b = q[i1:i2], actual[j1:j2]
        if tag in ("replace", "insert") and _has_content(a):
            extra.append(a.strip() or a)
        if tag in ("replace", "delete") and _has_content(b):
            missing.append(b.strip() or b)
    clip = lambda xs: xs[:MAX_DIFF_ITEMS] + (["…"] if len(xs) > MAX_DIFF_ITEMS else [])
    return clip(extra), clip(missing)


# ---------------------------------------------------------------- 读原文

def load_source_text(cfg: dict, source_id: str | None, abs_path: str,
                     clean: bool | None = None):
    """只读读取某篇的正文，并用**与建索引完全相同**的清洗流程处理。

    返回 ``(body_clean, meta, raw_text)``。绝不写入源库。
    """
    if clean is None:
        clean = True
        for s in cfg.get("sources", []):
            if s.get("id") == source_id:
                clean = bool(s.get("clean", True))
                break
    max_bytes = int((cfg.get("scan") or {}).get("max_file_bytes", 8_000_000))
    raw = indexer.read_bytes(abs_path, max_bytes)          # open(rb)：只读
    text = textproc.strip_bom(raw.decode("utf-8", errors="replace"))
    meta, body, _fm = textproc.parse_front_matter(text)
    m = textproc.extract_meta(meta, body, os.path.basename(abs_path))
    body_clean, _stats = textproc.clean_body(body, m.get("title", ""), clean)
    return body_clean, m, text


class _Cache:
    """一次批量核对里复用：篇正文、篇片段、全文检索候选。"""

    def __init__(self):
        self.body: dict[int, str] = {}
        self.raw: dict[int, str] = {}
        self._hay: dict[int, _Hay] = {}
        self._raw_hay: dict[int, _Hay] = {}
        self.chunks: dict[int, list[str]] = {}

    def load(self, cfg: dict, doc: dict) -> tuple[str, str]:
        did = int(doc["id"])
        if did not in self.body:
            try:
                body, _meta, raw = load_source_text(cfg, doc.get("source_id"),
                                                    doc["abs_path"])
            except FileNotFoundError:
                body, raw = "", ""
            except Exception as e:                       # noqa: BLE001
                body, raw = "", ""
                doc["_read_error"] = f"{type(e).__name__}: {e}"
            self.body[did], self.raw[did] = body, raw
            self._hay[did] = _Hay(body)
            self._raw_hay[did] = _Hay(raw)
        return self.body[did], self.raw[did]

    def body_hay(self, cfg: dict, doc: dict) -> _Hay:
        self.load(cfg, doc)
        return self._hay[int(doc["id"])]

    def raw_hay(self, cfg: dict, doc: dict) -> _Hay:
        self.load(cfg, doc)
        return self._raw_hay[int(doc["id"])]

    def chunk_texts(self, conn, doc_id: int) -> list[str]:
        if doc_id not in self.chunks:
            self.chunks[doc_id] = [r["text"] for r in conn.execute(
                "SELECT text FROM chunks WHERE doc_id=? ORDER BY seq", (doc_id,))]
        return self.chunks[doc_id]


# ---------------------------------------------------------------- 找篇

def _doc_rows_by_bvid(conn, bvid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        f"SELECT {_DOC_COLS_D} FROM docs d WHERE d.bvid=? ORDER BY d.source_id", (bvid,))]


def _doc_row_by_id(conn, doc_id) -> dict | None:
    try:
        did = int(doc_id)
    except (TypeError, ValueError):
        return None
    r = conn.execute(f"SELECT {_DOC_COLS_D} FROM docs d WHERE d.id=?", (did,)).fetchone()
    return dict(r) if r else None


def _candidate_docs(conn, quote: str, limit: int = MAX_CANDIDATE_DOCS) -> list[dict]:
    """用 FTS 找"这句话可能出自哪些篇"（只看已建索引的篇）。"""
    rows, _notes = search_mod.fts_search(
        conn, quote, 400, "d.state='indexed'", [], max_terms=32)
    out, seen = [], set()
    for cid, _score in rows:
        r = conn.execute(
            f"SELECT {_DOC_COLS_D} FROM docs d JOIN chunks c ON c.doc_id=d.id WHERE c.id=?",
            (cid,)).fetchone()
        if not r or r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def _seed_candidates(conn, quote: str, limit: int) -> list[dict]:
    """引文可能被改错了，FTS 整句查不到时：拿几个 4 字窗口当种子再试。"""
    t = _tight(quote)
    out, seen = [], set()
    step = max(1, len(t) // 4)
    for i in range(0, max(1, len(t) - 3), step):
        seed = t[i:i + 8]
        if len(seed) < 4:
            continue
        for d in _candidate_docs(conn, seed, limit - len(out)):
            if d["id"] not in seen:
                seen.add(d["id"]); out.append(d)
        if len(out) >= limit:
            break
    return out[:limit]


def _best_suggestion(cache: _Cache, cfg: dict, needle: str, doc: dict) -> dict | None:
    """在一篇的正文里找与 needle 最接近的窗口，返回可用于修正引文的原文片段。"""
    hay = cache.body_hay(cfg, doc)
    if not needle or not hay.tight:
        return None
    sm = difflib.SequenceMatcher(None, needle, hay.tight, autojunk=False)
    m = sm.find_longest_match(0, len(needle), 0, len(hay.tight))
    if m.size < 3:          # 中文 3 字以下的重合没有指向性，交给 _chunk_hint
        return None
    start = max(0, m.b - m.a)
    span = min(len(needle) + 24, len(hay.tight) - start)
    if span <= 0:
        return None
    win_tight = hay.tight[start:start + span]
    ratio = difflib.SequenceMatcher(None, needle, win_tight, autojunk=False).ratio()
    o_start = hay.tight_map[start]
    o_end = hay.tight_map[start + span - 1] + 1
    return {"ratio": round(float(ratio), 3), "text": hay.text[o_start:o_end],
            "offset": o_start, "rel_path": doc.get("rel_path"),
            "bvid": doc.get("bvid"), "doc_id": doc["id"], "title": doc.get("title") or ""}


def _focus_on_quote(quote: str, actual: str) -> str:
    """在建议片段里截出"对应引文的那一段"：从第一个有意义的匹配块到最后一个。

    建议片段是"引文长度 + 24 字"的窗口，直接拿来 diff 会把窗口尾巴全算成"引文缺字"，
    截一下才只报真正的多字/少字/改字。
    """
    q = _clean_quote(quote)
    sm = difflib.SequenceMatcher(None, q, actual, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size >= 3]
    if not blocks:
        return actual
    return actual[blocks[0].b:blocks[-1].b + blocks[-1].size]


# ---------------------------------------------------------------- 单条核对

def _chunk_hint(conn, quote: str, doc: dict, k: int = 1) -> list[dict]:
    """在指定的一篇里，按关键词找"最沾边的片段"当线索。

    引文被改写得太远、diff 找不到相近句时用这个兜底——
    至少告诉作者"你标注的那一篇里，跟这句话相关的内容是这样写的"。
    """
    try:
        rows, _notes = search_mod.fts_search(conn, quote, 5, "c.doc_id=?",
                                             [int(doc["id"])])
    except Exception:                                       # noqa: BLE001
        return []
    out = []
    for cid, _score in rows[:k]:
        r = conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
        if not r:
            continue
        out.append({"ratio": None, "text": r["text"][:400], "bvid": doc.get("bvid"),
                    "doc_id": doc["id"], "title": doc.get("title") or "",
                    "from_chunk": True})
    return out


def verify_quote(conn, cfg: dict, quote: str, bvid: str | None = None,
                 doc_id=None, fuzzy: bool = True, context_chars: int = 90,
                 max_suggestions: int = MAX_SUGGESTIONS, cache: _Cache | None = None
                 ) -> dict:
    """核对一条引文。``bvid``/``doc_id`` 可限定出处；不给则全库找。"""
    cache = cache if cache is not None else _Cache()
    q = _clean_quote(quote)
    res = {"quote": quote, "status": "not_found", "tier": None, "verdict": "",
           "ok": False, "bvid": None, "doc_id": None, "title": None, "rel_path": None,
           "offset": None, "occurrences": 0, "matched_text": "", "context": "",
           "in_chunk": None, "extra_in_quote": [], "missing_from_quote": [],
           "annotations": [], "stripped_used": False,
           "suggestions": [], "notes": []}
    if not _has_content(q) or len(_tight(q)) < MIN_QUOTE_CHARS:
        res.update(status="error", verdict=_VERDICT["error"])
        res["notes"].append(
            f"引文去掉空白与标点后不足 {MIN_QUOTE_CHARS} 字，无法核对（空引文/纯标点）")
        return res

    brackets = BRACKET_RE.findall(q)
    un_bracketed = _clean_quote(BRACKET_RE.sub("", q))
    can_strip = bool(brackets) and len(_tight(un_bracketed)) >= MIN_QUOTE_CHARS

    docs: list[dict] = []
    declared = bool(bvid) or doc_id is not None   # 用户有没有明确指出处
    if doc_id is not None:
        d = _doc_row_by_id(conn, doc_id)
        if d:
            docs = [d]
        else:
            res.update(status="error", verdict=_VERDICT["error"])
            res["notes"].append(f"doc_id={doc_id} 不在索引库里")
            return res
    elif bvid:
        docs = _doc_rows_by_bvid(conn, bvid)
        if not docs:
            res.update(status="error", verdict=_VERDICT["error"])
            res["notes"].append(
                f"bvid={bvid} 不在索引库里（用 list_documents 核对一下 BVID 拼写）")
            return res
    else:
        docs = _candidate_docs(conn, q)
        if not docs:
            res["notes"].append("全库 FTS 未找到任何候选篇目")

    # ---- 1) 在正文里找
    for d in docs:
        hay = cache.body_hay(cfg, d)
        stripped_used = False
        hit = hay.locate(q)
        if not hit and can_strip:
            # 整句对不上，但引文里有作者加的括注 → 去掉括注再试
            hit = hay.locate(un_bracketed)
            stripped_used = bool(hit)
        if not hit:
            continue
        start, end = hit["start"], hit["end"]
        actual = hay.text[start:end]
        extra, missing = ([], []) if hit["tier"] == "exact" else _diff_detail(q, actual)
        if stripped_used:
            annotations = list(brackets)
        else:
            # 逐字出现于原文的括注是原文自带的，不算作者插话
            annotations = [b for b in brackets if b not in actual]
        if extra:
            status = "modified"
        elif annotations:
            status = "annotated"
        else:
            status = {"exact": "verbatim", "whitespace": "whitespace",
                      "loose": "loose"}[hit["tier"]]
        if missing and status in ("verbatim", "whitespace", "loose", "annotated"):
            # 引文比原文短（漏字），也算实质差异
            status = "modified"
        res.update(
            status=status, tier=hit["tier"], verdict=_VERDICT[status],
            ok=status in ("verbatim", "whitespace", "loose", "annotated"),
            bvid=d.get("bvid"), doc_id=d["id"], title=d.get("title") or "",
            rel_path=d.get("rel_path"), offset=start, occurrences=hit["count"],
            matched_text=actual, stripped_used=stripped_used, annotations=annotations,
            context=hay.text[max(0, start - int(context_chars)):end + int(context_chars)],
            extra_in_quote=extra, missing_from_quote=missing,
            in_chunk=_in_chunk(conn, cache, d["id"], q if not stripped_used else un_bracketed))
        if annotations:
            res["notes"].append(
                "引文里有原文没有的括注：" + "、".join(annotations)
                + "。要么删掉，要么改用［］并在文中说明是补充解释——括注会让读者以为原文就这么说")
        if status == "modified":
            res["notes"].append("引文与原文对不上，请照 matched_text 改写；"
                                "多出/缺少的字已列在 extra_in_quote / missing_from_quote")
        if not res["in_chunk"]:
            res["notes"].append("这句跨了片段边界——只查索引会误判为查不到，"
                                "所以核对必须回原文")
        if hit["count"] > 1:
            res["notes"].append(f"这篇正文里这句话出现了 {hit['count']} 次")
        if not d.get("bvid"):
            res["notes"].append("该篇没有 BVID（可能是非 B站 来源）")
        return res

    # ---- 2) 指定了出处却没找到 → 看看原始文件里有没有（乱码表头/元数据区）
    for d in docs:
        hit = cache.raw_hay(cfg, d).locate(q)
        if not hit:
            continue
        res.update(status="not_in_body", tier="raw_only", verdict=_VERDICT["not_in_body"],
                   ok=False, bvid=d.get("bvid"), doc_id=d["id"],
                   title=d.get("title") or "", rel_path=d.get("rel_path"),
                   offset=hit["start"], occurrences=hit["count"],
                   context=cache.raw[d["id"]][max(0, hit["start"] - int(context_chars)):
                                               hit["end"] + int(context_chars)])
        res["notes"].append("命中位置在正文之外（乱码表头/元数据区），不能作为引文")
        return res

    # ---- 3) 指定的出处里没有 → 可能是标错了出处：去全库别的篇里找找
    others = []
    if doc_id is not None or bvid:
        known = {int(d["id"]) for d in docs}
        others = [d for d in _candidate_docs(conn, q) if int(d["id"]) not in known]
        for d in others:
            hay = cache.body_hay(cfg, d)
            hit = hay.locate(q) or (cache.body_hay(cfg, d).locate(un_bracketed)
                                    if can_strip else None)
            if not hit:
                continue
            actual = cache.body[d["id"]][hit["start"]:hit["end"]]
            res.update(status="other_doc", tier=hit["tier"], verdict=_VERDICT["other_doc"],
                       ok=False, bvid=d.get("bvid"), doc_id=d["id"],
                       title=d.get("title") or "", rel_path=d.get("rel_path"),
                       offset=hit["start"], occurrences=hit["count"], matched_text=actual,
                       in_chunk=_in_chunk(conn, cache, d["id"], q),
                       context=cache.body[d["id"]][
                           max(0, hit["start"] - int(context_chars)):hit["end"] + int(context_chars)])
            res["notes"].append(
                f"这句确实存在，但在 {d.get('bvid')}（{d.get('title')}）里，不是你标注的那篇——"
                "出处需要改")
            return res

    # ---- 4) 真找不到 → 给"原文大概是这么写的"的建议
    res.update(status="not_found", verdict=_VERDICT["not_found"])
    if fuzzy:
        cands = docs or others or _seed_candidates(conn, q, min(4, MAX_CANDIDATE_DOCS))
        needle = _tight(un_bracketed if can_strip else q)
        sug = []
        for d in cands:
            s = _best_suggestion(cache, cfg, needle, d)
            if s:
                sug.append(s)
        # 相似度太低的"建议"是噪声（只是碰巧共用几个词），宁可明说没找到
        sug = [s for s in sug if (s.get("ratio") or 0.0) >= SUGGEST_MIN_RATIO]
        sug.sort(key=lambda x: -(x.get("ratio") or 0.0))
        if not sug and declared:
            # diff 找不到相近句（引文被改得太远）→ 至少给"你标注那篇里沾边的片段"
            for d in docs[:2]:
                sug.extend(_chunk_hint(conn, q, d))
        res["suggestions"] = sug[:max_suggestions]

        # 库里有一句"几乎一模一样"的 → 这不是"库里查不到"，而是"这段文字就在库里，
        # 但你写的和原文不一样"。照 matched_text 改写即可。
        top = sug[0] if sug else None
        if top and not top.get("from_chunk"):
            focused = _focus_on_quote(un_bracketed if can_strip else q, top["text"])
            extra, missing = _diff_detail(un_bracketed if can_strip else q, focused)
            off = top.get("offset")
            res.update(status="modified", verdict=_VERDICT["modified"],
                       bvid=top.get("bvid"), doc_id=top["doc_id"],
                       title=top.get("title") or "", rel_path=top.get("rel_path"),
                       offset=off, matched_text=focused, extra_in_quote=extra,
                       missing_from_quote=missing,
                       in_chunk=_in_chunk(conn, cache, top["doc_id"],
                                          un_bracketed if can_strip else q))
            if off is not None:
                body = cache.body[int(top["doc_id"])]
                res["context"] = body[max(0, off - int(context_chars)):
                                      off + len(focused) + int(context_chars)]
            where = ("你标注的那一篇" if declared
                     else f"{top.get('bvid') or ''}《{top.get('title') or ''}》".strip())
            res["notes"].append(
                f"库里 {where} 有一句几乎一模一样的（相似度 {top['ratio']}），"
                "但你写的和它不一样——请照 matched_text 改写，"
                "多出/缺少的字已列在 extra_in_quote / missing_from_quote")
            return res

        if sug and sug[0].get("from_chunk"):
            res["notes"].append(
                "没找到相近的句子；建议里给的是你标注那篇中与引文关键词最接近的片段，"
                "照着它重新组织引文")
        elif sug:
            res["notes"].append(
                f"最接近的原文在第 1 条建议里（相似度 {sug[0]['ratio']}），请照它改写引文")
        else:
            res["notes"].append("连相近的句子都没找到——这句很可能是自撰的")
        if not declared:
            res["notes"].append(
                "若你知道这句出自哪一篇，把 bvid（或 doc_id）一起给我，"
                "我能更准地指出最接近的原文是在哪一篇的哪一段")
    return res


def _in_chunk(conn, cache: _Cache, doc_id: int, quote: str) -> bool:
    """这句是否完整落在某个片段里（用来暴露"跨片段"这种索引侧假阴性）。"""
    needle = _tight(quote)
    for t in cache.chunk_texts(conn, doc_id):
        if needle in _tight(t):
            return True
    return False


# ---------------------------------------------------------------- 批量核对

def verify_quotes(conn, cfg: dict, quotes, bvid: str | None = None, doc_id=None,
                  fuzzy: bool = True, context_chars: int = 90, split: bool = True,
                  max_suggestions: int = MAX_SUGGESTIONS) -> dict:
    """批量核对。``quotes`` 可以是字符串列表，也可以是
    ``[{"quote": "...", "bvid": "BV..."}, ...]`` 这样的字典列表。"""
    if isinstance(quotes, str):
        quotes = [quotes]
    cache = _Cache()
    items = []
    for raw in (quotes or []):
        spec = raw if isinstance(raw, dict) else {"quote": str(raw)}
        q = spec.get("quote") or spec.get("text") or ""
        b = spec.get("bvid") or bvid
        did = spec.get("doc_id", doc_id)
        frags, had_ellipsis = (split_ellipsis(q) if split else ([_clean_quote(q)], False))
        parts = [verify_quote(conn, cfg, f, bvid=b, doc_id=did, fuzzy=fuzzy,
                              context_chars=context_chars,
                              max_suggestions=max_suggestions, cache=cache)
                 for f in frags]
        worst = max(parts, key=lambda p: _SEV.get(p["status"], 9))
        status = worst["status"]
        item = {
            "quote": q, "status": status, "verdict": _VERDICT.get(status, ""),
            "ok": all(p["ok"] for p in parts)
                  and status in ("verbatim", "whitespace", "loose", "annotated"),
            "had_ellipsis": had_ellipsis, "fragments": parts,
            "annotations": [a for p in parts for a in (p.get("annotations") or [])],
        }
        # 汇总：BVID 集合、命中次数、主要建议
        item["bvid"] = worst.get("bvid")
        item["doc_id"] = worst.get("doc_id")
        item["title"] = worst.get("title")
        item["matched_text"] = worst.get("matched_text") or ""
        item["offset"] = worst.get("offset")
        item["in_chunk"] = worst.get("in_chunk")
        item["notes"] = list(worst.get("notes") or [])
        item["suggestions"] = worst.get("suggestions") or []
        if had_ellipsis and len(parts) > 1:
            item["notes"] = list(item["notes"]) + [
                f"引文含省略号，已拆成 {len(parts)} 段分别核对"]
        items.append(item)

    n = len(items)
    bad = [i for i in items if not i["ok"]]
    hard = [i for i in items if i["status"] in ("not_found", "not_in_body", "error")]
    return {
        "total": n,
        "verbatim": sum(1 for i in items if i["status"] == "verbatim"),
        "warn": sum(1 for i in items if i["status"] in ("whitespace", "loose", "annotated")),
        "modified": sum(1 for i in items if i["status"] == "modified"),
        "failed": len(hard),
        "ok": not bad,
        "failed_quotes": [i["quote"] for i in hard][:10],
        "results": items,
    }


# ---------------------------------------------------------------- 整篇文稿

def extract_quotes(md_text: str) -> list[dict]:
    """从 Markdown 里抽出引文：块引用行(> )里的「…」「『…』」，并关联上文最近的 BVID。

    只抽带引号的片段——正文里的普通句子不算引文。
    """
    out = []
    last_bvid, last_line = None, 0
    for idx, line in enumerate((md_text or "").splitlines(), 1):
        for m in BV_RE.finditer(line):
            last_bvid, last_line = m.group(0), idx
        if not line.lstrip().startswith(">"):
            continue
        for seg in _scan_quoted(line):
            seg = _clean_quote(seg)
            if not _has_content(seg) or len(_tight(seg)) < MIN_QUOTE_CHARS:
                continue
            out.append({"quote": seg, "bvid": last_bvid, "line": idx,
                        "bvid_line": last_line})
    return out


def _scan_quoted(line: str) -> list[str]:
    """按「」『』配对切出引文段（允许内部嵌套另一种引号）。"""
    pairs = {"「": "」", "『": "』", "“": "”"}
    segs, i = [], 0
    while i < len(line):
        ch = line[i]
        if ch in pairs:
            j = line.find(pairs[ch], i + 1)
            if j < 0:
                break
            segs.append(line[i + 1:j])
            i = j + 1
        else:
            i += 1
    return segs


def verify_article(conn, cfg: dict, md_text: str, bvid: str | None = None,
                   fuzzy: bool = True, context_chars: int = 90) -> dict:
    """核对整篇 Markdown 里所有引文，按行号汇总。"""
    found = extract_quotes(md_text)
    if not found:
        return {"total": 0, "ok": True, "results": [], "by_line": [],
                "note": "文稿里没找到带引号的引文（用「」『』“”包起来的片段）"}
    res = verify_quotes(conn, cfg,
                        [{"quote": f["quote"], "bvid": bvid or f["bvid"]} for f in found],
                        bvid=bvid, fuzzy=fuzzy, context_chars=context_chars)
    for item, src in zip(res["results"], found):
        item["line"] = src["line"]
        item["declared_bvid"] = src["bvid"]
        item["bvid_line"] = src["bvid_line"]
        if src["bvid"] and item["bvid"] and src["bvid"] != item["bvid"] and item["ok"]:
            item["notes"] = list(item.get("notes") or []) + [
                f"文稿里标注的是 {src['bvid']}，但这句话实际出自 {item['bvid']}"]
            item["ok"] = False
            item["status"] = "other_doc"
            item["verdict"] = _VERDICT["other_doc"]
    res["by_line"] = [{"line": i["line"], "bvid": i.get("bvid"),
                       "status": i["status"], "ok": i["ok"]} for i in res["results"]]
    res["ok"] = all(i["ok"] for i in res["results"])
    return res