# -*- coding: utf-8 -*-
"""kb-mcp：本地知识库 MCP 服务（stdio）。

模块划分：
  config    配置加载（config.example.json 默认 + config.json 覆盖）
  textproc  纯内存文本处理：frontmatter 解析 / 只读清洗 / 切块
  store     SQLite 存储（FTS5 trigram 全文 + 向量 blob）
  embed     本地 ONNX 向量化（bge-small-zh-v1.5）
  search    混合检索（FTS5 + 向量，RRF 融合）
  indexer   增量索引（只读扫描源库）
  server    MCP stdio JSON-RPC 服务
  cli       命令行入口
"""

__version__ = "0.1.0"