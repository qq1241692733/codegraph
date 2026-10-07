"""impact：爆炸半径分析（借鉴 GitNexus 的 impact 工具）。

输入一个符号 FQN，返回结构化报告：上游调用者 + 下游被调用者，按深度分组，
并归并到受影响文件。结构可 JSON 序列化，供 Deep Agents 工具直接返回。
"""
from __future__ import annotations

from pathlib import Path

from . import db


def run_impact(db_path: str | Path, symbol: str, depth: int = 2) -> dict:
    conn = db.connect(db_path)
    upstream = db.find_callers(conn, symbol, depth)
    downstream = db.find_callees(conn, symbol, depth)
    conn.close()
    return _build_report(symbol, upstream, downstream, depth)


def _group(items: list[dict], key: str) -> dict:
    """按深度分组 + 归并受影响文件。"""
    by_depth: dict[int, list[str]] = {}
    files: set[str] = set()
    for it in items:
        by_depth.setdefault(it["depth"], []).append(it[key])
        files.add(it[key].split(".")[0])
    return {
        "total": len({it[key] for it in items}),
        "by_depth": {str(k): v for k, v in by_depth.items()},
        "files": sorted(files),
    }


def _build_report(symbol: str, upstream: list[dict], downstream: list[dict], depth: int) -> dict:
    up = _group(upstream, "caller")
    down = _group(downstream, "callee")
    return {
        "symbol": symbol,
        "depth": depth,
        "upstream": up,
        "downstream": down,
        "summary": (
            f"改动 {symbol} 会波及 {up['total']} 个上游调用者"
            f"（分布在 {len(up['files'])} 个文件）和 {down['total']} 个下游被调用者"
            f"（分布在 {len(down['files'])} 个文件）。"
        ),
    }
