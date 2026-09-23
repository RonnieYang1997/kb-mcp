# -*- coding: utf-8 -*-
"""索引入口（可与 kb_serve.py 同一个解释器；供 daily_scan.py 尾部与后台全量调用）。

用法：
  <python> bin/kb_index.py                 # 增量
  <python> bin/kb_index.py --full          # 全量（后台任务用）
  <python> bin/kb_index.py --job-id abc123 # 更新 jobs 表进度
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from kb.cli import main  # noqa: E402

if __name__ == "__main__":
    argv = sys.argv[1:]
    raise SystemExit(main(["index"] + argv))