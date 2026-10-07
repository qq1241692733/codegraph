"""M4 本地 embedding 后端：fastembed + onnxruntime 跑多语言模型。

- 用多语言模型（paraphrase-multilingual-MiniLM-L12-v2），让**中文 query 能对齐英文代码语义**。
- 纯本地、免费、无云 API；首次用会下载模型权重到本地缓存（~470MB）。
- 后端可插拔：加载失败则 semantic 为空，hybrid 退化为纯 BM25（不崩）。
"""
from __future__ import annotations

import numpy as np

# 多语言：中文 query 对齐英文代码片段
MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def make_embedder():
    """返回 embed(texts: list[str]) -> np.ndarray (N, D)。加载失败返回 None。"""
    try:
        from fastembed import TextEmbedding
    except Exception:
        return None
    try:
        model = TextEmbedding(model_name=MODEL_NAME)
    except Exception:
        return None

    def embed(texts: list[str]) -> np.ndarray:
        rows = [np.asarray(v, dtype=np.float32) for v in model.embed(list(texts))]
        return np.vstack(rows) if rows else np.zeros((0, model.model_output_length or 0), dtype=np.float32)

    return embed


def attach_semantic(index: dict, embed) -> None:
    """把向量后端挂到检索索引：预计算 doc 向量，语义检索按余弦相似度召回。"""
    docs = index["docs"]
    if not docs:
        index["semantic"] = lambda query, k: []
        return
    texts = [d["text"] for d in docs]
    vecs = embed(texts)
    norms = np.linalg.norm(vecs, axis=1)

    def backend(query: str, k: int = 10) -> list[tuple[str, float]]:
        if not query.strip():
            return []
        qv = embed([query])[0]
        dots = vecs @ qv
        sims = dots / (norms * np.linalg.norm(qv) + 1e-9)
        order = np.argsort(-sims)[:k]
        return [(docs[i]["id"], float(sims[i])) for i in order]

    index["semantic"] = backend
