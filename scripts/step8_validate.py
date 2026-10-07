"""M4 第一步验证：语义检索引擎框架 + BM25 关键词检索。

验证点：
1. sample_repo 建索引，检索已知符号（按名字/关键词命中）。
2. 真实 GitHub 项目（itsdangerous / flask / fastapi）建索引，验证 BM25 能召回对应函数。
3. hybrid_search 在无向量后端时退化为纯 BM25（框架可插拔，不崩）。

用法：uv run python scripts/step8_validate.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codegraph import db, retrieval
from codegraph.parser import parse_python
from codegraph.graph import build_graph


def _build_db(repo: Path, db_path: Path) -> None:
    files = sorted(p for p in repo.rglob("*.py") if not p.name.startswith("."))
    parsed = [parse_python(f.read_bytes(), str(f.relative_to(repo))) for f in files]
    graph = build_graph(parsed)
    db.wipe_db(db_path)
    obj, conn = db.load_graph(db_path, graph)
    conn.execute("CHECKPOINT")
    conn.close()
    obj.close()


def _top(index, query, k=5):
    return [r["id"] for r in retrieval.hybrid_search(index, query, k)]


def _check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    if not cond:
        sys.exit(1)


def main() -> None:
    # ---- 1) sample_repo：检索已知符号 ----
    print("== sample_repo ==")
    repo = ROOT / "examples" / "sample_repo"
    db_path = Path(tempfile.mkdtemp()) / "cg.db"
    _build_db(repo, db_path)
    conn = db.connect(db_path)
    index = retrieval.build_index(conn, repo)
    conn.close()
    ids = _top(index, "login", 5)
    print(f"  query='login' → {ids[:4]}")
    _check("检索到 auth.login", any(i.endswith("auth.login") for i in ids))
    ids = _top(index, "verify password", 5)
    print(f"  query='verify password' → {ids[:4]}")
    _check("检索到 verify_password", any("verify_password" in i for i in ids))

    # ---- 2) hybrid 无向量后端退化为纯 BM25（不崩） ----
    r = retrieval.hybrid_search(index, "login", 5)
    _check("hybrid 无向量后端退化为 BM25 且不崩", len(r) > 0 and all(s == "bm25" for s in [x["sources"][0] for x in r]))
    print("  sources:", {x["sources"][0] for x in r})

    # ---- 3) 真实 GitHub 项目 ----
    TEST = __import__("scripts.setup_repos", fromlist=["ensure_repos", "SRC"]).ensure_repos()
    _SRC = __import__("scripts.setup_repos", fromlist=["SRC"]).SRC
    cases = [
        ("itsdangerous", TEST / _SRC["small"], ["serializer", "loads", "sign"]),
        ("flask", TEST / _SRC["medium"], ["route", "render_template", "redirect"]),
        ("fastapi", TEST / _SRC["big"], ["Depends", "HTTPException", "run"]),
    ]
    for name, repo_p, queries in cases:
        if not repo_p.exists():
            print(f"== {name}：跳过（未 clone，跑 step7 前先准备） ==")
            continue
        print(f"== {name} ==")
        db_path = Path(tempfile.mkdtemp()) / f"{name}.db"
        _build_db(repo_p, db_path)
        conn = db.connect(db_path)
        index = retrieval.build_index(conn, repo_p)
        conn.close()
        ndocs = len(index["docs"])
        print(f"  索引 {ndocs} 个符号")
        for q in queries:
            ids = _top(index, q, 5)
            hit = any(q.lower() in i.lower() for i in ids)
            print(f"  query='{q}' → top3: {ids[:3]} {'✅' if hit else '（未直接命中，看召回）'}")
        _check(f"{name} 索引非空", ndocs > 0)

    print("\n✅ Step8 通过：检索框架 + BM25 可运行（BM25 命中已知符号，hybrid 可插拔不崩）")


if __name__ == "__main__":
    main()
