# -*- coding: utf-8 -*-
"""本地 ONNX 向量化：bge-small-zh-v1.5（CLS pooling + L2 归一化）。

- 依赖 numpy / onnxruntime / tokenizers，全部装在 .venv 里。
- 模型缺失时 available=False，调用方降级为纯全文检索（服务照常可用）。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

MODEL_NAME = "bge-small-zh-v1.5"
_ONNX_CANDIDATES = ("model.onnx", "onnx/model.onnx", "model_quantized.onnx",
                    "onnx/model_quantized.onnx")


class Embedder:
    def __init__(self, model_dir: str, dim: int = 512, max_tokens: int = 512, batch: int = 32,
                 model_file: str = ""):
        self.model_dir = Path(model_dir)
        self.dim = int(dim)
        self.max_tokens = int(max_tokens)
        self.batch = max(1, int(batch))
        self.model_file = model_file
        self.available = False
        self.reason = "not loaded"
        self._tok = None
        self._sess = None
        self._inputs: list[str] = []

    # ---------- 加载 ----------

    def load(self) -> "Embedder":
        tok_path = None
        for cand in ("tokenizer.json", "onnx/tokenizer.json"):
            p = self.model_dir / cand
            if p.exists():
                tok_path = p
                break
        onnx_path = None
        candidates = ([self.model_file] if self.model_file else []) + list(_ONNX_CANDIDATES)
        for cand in candidates:
            if not cand:
                continue
            p = self.model_dir / cand
            if p.exists():
                onnx_path = p
                break
        if tok_path is None or onnx_path is None:
            missing = []
            if tok_path is None:
                missing.append("tokenizer.json")
            if onnx_path is None:
                missing.append("model.onnx")
            self.reason = "模型文件缺失: " + ", ".join(missing) + f" (目录 {self.model_dir})"
            return self
        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except Exception as e:  # pragma: no cover
            self.reason = f"依赖缺失: {type(e).__name__}: {e}"
            return self
        try:
            tok = Tokenizer.from_file(str(tok_path))
            tok.enable_truncation(max_length=self.max_tokens)
            tok.enable_padding(pad_id=0, pad_token="[PAD]")
            sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
            self._tok = tok
            self._sess = sess
            self._inputs = [i.name for i in sess.get_inputs()]
            self.available = True
            self.reason = "ok"
        except Exception as e:
            self.reason = f"加载失败: {type(e).__name__}: {e}"
        return self

    @property
    def model_id(self) -> str:
        return MODEL_NAME

    # ---------- 编码 ----------

    def encode(self, texts: list[str], prefix: str = "") -> np.ndarray:
        """返回 (n, dim) 的 L2 归一化 float32 矩阵。不可用时抛 RuntimeError。"""
        if not self.available:
            raise RuntimeError(f"embedder 不可用: {self.reason}")
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch):
            part = [prefix + t for t in texts[i:i + self.batch]]
            encs = self._tok.encode_batch(part)
            feed = {}
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed["input_ids"] = ids
            feed["attention_mask"] = mask
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            feed = {k: v for k, v in feed.items() if k in self._inputs}
            res = self._sess.run(None, feed)
            arr = res[0]
            if arr.ndim == 3:            # (b, seq, dim) → CLS pooling
                vec = arr[:, 0, :]
            elif arr.ndim == 2:          # 已经是 pooled
                vec = arr
            else:
                raise RuntimeError(f"未知输出形状: {arr.shape}")
            vec = np.asarray(vec, dtype=np.float32)
            out.append(vec)
        mat = np.vstack(out)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return (mat / norms).astype(np.float32)


def load_from_config(cfg: dict) -> Embedder:
    emb = cfg.get("embed", {})
    e = Embedder(emb.get("model_dir", ""), emb.get("dim", 512),
                 emb.get("max_tokens", 512), emb.get("batch", 32),
                 emb.get("model_file", ""))
    if not emb.get("enabled", True):
        e.reason = "配置中已禁用向量检索 (embed.enabled=false)"
        return e
    return e.load()


def model_status(cfg: dict) -> dict:
    emb = cfg.get("embed", {})
    d = Path(emb.get("model_dir", ""))
    files = {}
    for name in ("tokenizer.json", "model.onnx", "onnx/model.onnx",
                 "onnx/model_quantized.onnx", emb.get("model_file", "")):
        if not name:
            continue
        p = d / name
        files[name] = p.stat().st_size if p.exists() else 0
    return {
        "model_dir": str(d),
        "exists": d.exists(),
        "files": files,
        "model_file_config": emb.get("model_file", "") or "(自动：优先 model.onnx)",
        "total_bytes": sum(files.values()),
        "enabled": bool(emb.get("enabled", True)),
        "dim": emb.get("dim", 512),
    }