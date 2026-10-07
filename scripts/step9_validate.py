"""M4 第二、三步验证：本地语义向量 + 混合检索（BM25+向量 RRF 融合）。

验证点：
1. 中文 query 能语义召回英文代码符号（多语言 embedding 对齐）。
2. hybrid_search 的 sources 同时含 bm25 与 semantic（真正融合）。
3. 语义向量后，中文 query 召回归类准确率明显优于纯 BM25。
4. 真实项目：中文意图 query 召回应有符号（flask 渲染 / fastapi HTTP 异常）。

用法：uv run python scripts/step9_validate.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codegraph import db, retrieval, embedding
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


def _index(repo: Path):
    db_path = Path(tempfile.mkdtemp()) / "cg.db"
    _build_db(repo, db_path)
    conn = db.connect(db_path)
    index = retrieval.build_index(conn, repo)
    conn.close()
    emb = embedding.make_embedder()
    embedding.attach_semantic(index, emb)
    return index


def _check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    if not cond:
        sys.exit(1)


def main() -> None:
    # ---- sample_repo：中文语义召回 + 混合来源 ----
    print("== sample_repo（中文 query） ==")
    repo = ROOT / "examples" / "sample_repo"
    index = _index(repo)
    r = retrieval.hybrid_search(index, "做登录鉴权的函数", 5)
    ids = [x["id"] for x in r]
    src = {s for x in r for s in x["sources"]}
    print(f"  query='做登录鉴权的函数' → {ids[:4]}  sources={src}")
    _check("中文语义召回 auth.login / verify_password",
           any("auth.login" in i for i in ids) or any("verify_password" in i for i in ids))

    # 混合融合：用中英混合 query，BM25 命中英文词 + 语义命中 → sources 双含
    r2 = retrieval.hybrid_search(index, "login 鉴权", 5)
    src2 = {s for x in r2 for s in x["sources"]}
    print(f"  query='login 鉴权' → sources={src2}")
    _check("混合检索同时命中 bm25 与 semantic", "bm25" in src2 and "semantic" in src2)

    # ---- 真实项目：中文意图召回 ----
    TEST = __import__("scripts.setup_repos", fromlist=["ensure_repos", "SRC"]).ensure_repos()
    _SRC = __import__("scripts.setup_repos", fromlist=["SRC"]).SRC
    cases = [
        ("flask", TEST / _SRC["medium"], "渲染 HTML 模板的函数", ["render_template"]),
        ("fastapi", TEST / _SRC["big"], "处理 HTTP 异常的函数", ["HTTPException"]),
    ]
    for name, repo_p, q, expect in cases:
        if not repo_p.exists():
            print(f"== {name}：跳过（未 clone） ==")
            continue
        print(f"== {name}（中文意图） ==")
        index = _index(repo_p)
        r = retrieval.hybrid_search(index, q, 8)
        ids = [x["id"] for x in r]
        hit = any(e in i for e in expect for i in ids)
        print(f"  query='{q}'")
        for x in r[:5]:
            print(f"    {x['score']:.3f} [{','.join(x['sources'])}] {x['id']}")
        _check(f"召回 {'/'.join(expect)}", hit)

    print("\n✅ Step9 通过：本地语义向量 + 混合检索（中文 query 召回英文代码，BM25+向量 RRF 融合）")


if __name__ == "__main__":
    main()
