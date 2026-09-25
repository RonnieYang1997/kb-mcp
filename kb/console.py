# -*- coding: utf-8 -*-
"""控制台编码：让被重定向/管道的输出统一成 UTF-8。

背景：Windows 上 Python 往终端写走 WriteConsoleW，中文本来没问题；
但一旦 stdout 被重定向（shell 的 ``>``、``|``、kb.cmd 里），
Python 会退回 locale 编码（cp936），下游按 UTF-8 读就是乱码，
更糟的是遇到 "✗"、"✅" 这类字符会直接 UnicodeEncodeError 崩掉。

所以：**只在不是终端时**改写编码，交互式控制台保持原样。
"""
from __future__ import annotations

import sys


def force_utf8_when_piped() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if (hasattr(stream, "reconfigure") and not stream.isatty()
                    and enc != "utf8"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                                   # noqa: BLE001
            pass