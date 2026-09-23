# -*- coding: utf-8 -*-
"""配置加载：config.example.json 提供默认值，config.json 覆盖本机路径。

- 环境变量 KB_CONFIG 可指定另一份配置文件（自测用，避免污染正式配置）。
- 本模块只读配置文件；save() 只在用户显式调用 add-source 时写 config.json。
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_PATH = ROOT / "config.example.json"


def config_path() -> Path:
    env = os.environ.get("KB_CONFIG")
    return Path(env) if env else (ROOT / "config.json")


def default_db_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return str(Path(base) / "kb-mcp" / "index.db")


def default_model_dir() -> str:
    return str(ROOT / "models" / "bge-small-zh-v1.5")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    with open(EXAMPLE_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cp = config_path()
    if cp.exists():
        with open(cp, "r", encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    cfg["_config_path"] = str(cp)

    if not cfg.get("db_path"):
        cfg["db_path"] = default_db_path()
    emb = cfg.setdefault("embed", {})
    if not emb.get("model_dir"):
        emb["model_dir"] = default_model_dir()

    for s in cfg.get("sources", []):
        s.setdefault("id", Path(s["root"]).name)
        s.setdefault("label", s["id"])
        s.setdefault("read_only", True)
        s.setdefault("clean", True)
        s.setdefault("include", ["**/dufu-BV*.md"])
        s.setdefault("exclude", [])
    return cfg


def save(cfg: dict) -> None:
    """只写 config.json（本机配置），且绝不写任何源库。"""
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    cp = config_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    with open(cp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")


def source_by_id(cfg: dict, source_id: str | None) -> dict | None:
    if not source_id:
        return None
    for s in cfg.get("sources", []):
        if s["id"] == source_id:
            return s
    return None