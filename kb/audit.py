# -*- coding: utf-8 -*-
"""只读自审：静态扫描本服务代码，列出所有「可能写盘」的调用点，人工核对是否碰到源库。

产出是给用户看的证据：kb doctor / tools/audit_readonly.py 都会打印这份清单。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = [
    (re.compile(r"""open\([^)]*?['"](?:w|a|x|w\+|a\+|x\+|r\+|rb\+|wb)[b+]*['"]"""),
     "以写/追加模式打开文件"),
    (re.compile(r"\.write_text\(|\.write_bytes\(|\.touch\(|\.unlink\(|\.rename\("),
     "Path 写操作"),
    (re.compile(r"\bos\.remove\(|\bos\.unlink\(|\bos\.rename\(|\bos\.replace\(|\bos\.rmdir\(|"
                r"\bos\.mkdir\(|\bos\.makedirs\(|\bshutil\."),
     "文件系统写操作"),
    (re.compile(r"\bsubprocess\.|os\.system\("), "调用子进程（需核对不会写源库）"),
    (re.compile(r"\bexecutescript\(|\bexecutemany\(|\bCOMMIT\b|\.commit\(\)"), "数据库写入"),
]

ALLOWED = {
    # 这些写操作的对象是「索引库目录 / 日志目录」，都在源库之外
    "kb/store.py": "索引库目录（%LOCALAPPDATA%\\kb-mcp）与索引库自身",
    "kb/config.py": "本服务自己的 config.json（仓库内）",
    "kb/server.py": "后台索引进程的日志文件（%LOCALAPPDATA%\\kb-mcp\\logs）",
}

# 按「操作类型」放行的文件（写对象固定是索引库，位于源库之外）
DB_WRITERS = {"kb/store.py", "kb/indexer.py", "kb/cli.py", "kb/server.py"}
ALLOWED_BY_KIND = {
    "数据库写入": DB_WRITERS,
    "文件系统写操作": {"kb/store.py", "kb/server.py"},   # 仅 mkdir 索引库目录 / 日志目录
}


def scan(paths: list[Path] | None = None) -> list[dict]:
    findings: list[dict] = []
    files = paths if paths else sorted((ROOT / "kb").glob("*.py")) + sorted((ROOT / "bin").glob("*.py"))
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for pat, why in PATTERNS:
                if pat.search(line):
                    rel = str(p.relative_to(ROOT)).replace("\\", "/")
                    allowed = ALLOWED.get(rel, "")
                    if not allowed and rel in ALLOWED_BY_KIND.get(why, set()):
                        allowed = "写对象是索引库目录（%LOCALAPPDATA%\\kb-mcp），位于源库之外"
                    findings.append({"file": rel, "line": lineno, "kind": why,
                                     "code": line.strip()[:160],
                                     "allowed_because": allowed})
                    break
    return findings


def summarize() -> dict:
    findings = scan()
    unexplained = [f for f in findings if not f["allowed_because"]]
    return {
        "scanned_files": [str(p.relative_to(ROOT)).replace("\\", "/")
                          for p in sorted((ROOT / "kb").glob("*.py")) + sorted((ROOT / "bin").glob("*.py"))],
        "findings": findings,
        "unexplained": unexplained,
        "clean": len(unexplained) == 0,
        "rule": ("源库只读：kb 包内所有源文件读取均为 open(path,'rb')/os.stat/os.walk；"
                 "唯一的写操作都指向索引库目录与日志目录（均在源库之外）。"),
    }