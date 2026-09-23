# -*- coding: utf-8 -*-
"""MCP stdio 入口（绝对路径可调用，不依赖 cwd）。

AionUi 的 MCP 注册命令即：<仓库>\\.venv\\Scripts\\python.exe <仓库>\\bin\\kb_serve.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kb.server import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())