"""Deep Agents 工具层：把图谱分析暴露成 deepagents 可调用的工具。

设计：核心分析逻辑（impact/context/query）是纯函数、不依赖 deepagents；
只有 build_agent() 才惰性 import deepagents 并注册为 @tool。这样：
  - 没有 LLM/模型时，分析层照常可用；
  - 有模型时，一行 build_agent(...) 就得到架构感知的 Agent。

典型用法：
    agent = build_agent(model="openai:gpt-4o", db_path="codegraph.db")
    await agent.run("把 auth.login 改名，先告诉我影响范围")
"""
from __future__ import annotations

import json
from pathlib import Path

from . import db
from .impact import run_impact


def impact_payload(symbol: str, depth: int, db_path: str) -> dict:
    return run_impact(db_path, symbol, depth)


def context_payload(symbol: str, db_path: str) -> dict:
    conn = db.connect(db_path)
    ups = db.find_callers(conn, symbol, depth=1)
    downs = db.find_callees(conn, symbol, depth=1)
    conn.close()
    return {
        "symbol": symbol,
        "callers": [u["caller"] for u in ups],
        "callees": [d["callee"] for d in downs],
    }


def query_payload(text: str, db_path: str, limit: int = 10) -> dict:
    conn = db.connect(db_path)
    hits = db.search_symbols(conn, text, limit)
    conn.close()
    return {"query": text, "symbols": hits}


def semantic_search_payload(query: str, repo: str, db_path: str, k: int = 8) -> dict:
    """语义检索：按关键词/意思（中文也可）找代码符号，BM25 + 本地向量 RRF 融合。"""
    from . import retrieval
    from . import embedding

    conn = db.connect(db_path)
    index = retrieval.build_index(conn, Path(repo))
    conn.close()
    emb = embedding.make_embedder()  # 惰性加载本地向量模型
    if emb:
        embedding.attach_semantic(index, emb)
    results = retrieval.hybrid_search(index, query, k)
    return {"query": query, "mode": "hybrid" if index.get("semantic") else "bm25", "results": results}


def build_agent(model, db_path: str = "codegraph.db", repo: str = ".", **create_kwargs):
    """用 deepagents 构造架构感知 Agent，注册 impact/context/query/semantic_search 四个工具。"""
    from deepagents import create_deep_agent
    from langchain_core.tools import tool

    @tool
    def impact(symbol: str, depth: int = 2) -> str:
        """改动某符号会波及哪些调用者与被调用者（爆炸半径）。返回 JSON。"""
        return json.dumps(impact_payload(symbol, depth, db_path), ensure_ascii=False)

    @tool
    def context(symbol: str) -> str:
        """某符号的直接上游调用者与下游被调用者。返回 JSON。"""
        return json.dumps(context_payload(symbol, db_path), ensure_ascii=False)

    @tool
    def query(text: str) -> str:
        """按名字片段搜索代码符号。返回 JSON。"""
        return json.dumps(query_payload(text, db_path), ensure_ascii=False)

    @tool
    def semantic_search(text: str, k: int = 8) -> str:
        """按关键词或意思（支持中文）搜索代码符号，返回候选符号、所在文件与命中来源。"""
        return json.dumps(semantic_search_payload(text, repo, db_path, k), ensure_ascii=False)

    return create_deep_agent(
        model=model,
        tools=[impact, context, query, semantic_search],
        system_prompt=(
            "你是具备架构感知的代码 Agent。改动任何符号前，必须先调用 impact "
            "确认爆炸半径；不确定符号名时先用 semantic_search（支持中文描述）或 query 定位；"
            "要了解某符号的调用关系用 context。"
        ),
        **create_kwargs,
    )
