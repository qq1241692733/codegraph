"""CLI 入口：analyze（建图落库）/ impact（爆炸半径）/ callers / callees / search（语义检索）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .graph import build_graph
from .parser import discover_sources, parse_many, language_of
from . import db
from . import incremental
from .impact import run_impact
from . import retrieval
from . import embedding


def _analyze(repo: str, db_path: str, force: bool = False) -> None:
    root = Path(repo)
    snapshot_path = f"{db_path}.snapshot.json"
    if not force:
        st = incremental.run_incremental(db_path, root, snapshot_path)
        if st["mode"] == "incremental":
            print(f"增量写回：重解析 {st['reparsed']} 个文件（其中 importer {st['importers']} 个），"
                  f"新增 {st['added']}，修改 {st['changed']}，删除 {st['deleted']}，未解析 {st['unresolved']}")
            print(f"图谱已更新：{db_path}")
            return
        if st["mode"] == "up-to-date":
            print("无变更，图谱已是最新（跳过写回）")
            return

    files = [Path(x) for x in discover_sources(root)]
    if not files:
        print(f"未在 {repo} 找到支持的文件（.py/.js/.ts/.tsx/.java）")
        sys.exit(1)
    db.wipe_db(db_path)  # 全量重建前清掉旧库（兼容单文件/目录），避免 catalog 残留
    parsed = parse_many(files, root)
    graph = build_graph(parsed)
    db_obj, conn = db.load_graph(db_path, graph)
    conn.execute("CHECKPOINT")  # 确保 WAL 落盘，避免依赖进程退出的隐式 flush
    conn.close()
    db_obj.close()
    incremental.save_snapshot(snapshot_path, incremental.sha1_files(root))

    n_call = sum(1 for e in graph.edges if e.type == "CALLS")
    n_sym = sum(1 for n in graph.nodes if n.label in ("Function", "Class"))
    print(f"已索引 {len(files)} 个文件，{n_sym} 个符号，{len(graph.edges)} 条边（其中 CALLS {n_call}）")
    print(f"未解析调用：{getattr(graph, 'props_unresolved', 0)}")
    print(f"图谱已写入：{db_path}")


def _impact(db_path: str, symbol: str, depth: int) -> None:
    report = run_impact(db_path, symbol, depth)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _callers_callees(db_path: str, symbol: str, depth: int, kind: str) -> None:
    conn = db.connect(db_path)
    fn = db.find_callers if kind == "callers" else db.find_callees
    for row in fn(conn, symbol, depth):
        print(f"  [depth {row['depth']}] {row['caller'] if kind == 'callers' else row['callee']}")
    conn.close()


def _search(db_path: str, repo: str, query: str, k: int) -> None:
    conn = db.connect(db_path)
    index = retrieval.build_index(conn, Path(repo))
    conn.close()
    emb = embedding.make_embedder()
    if emb:
        embedding.attach_semantic(index, emb)
        note = "BM25 + 语义向量"
    else:
        note = "纯 BM25（未加载向量后端）"
    results = retrieval.hybrid_search(index, query, k)
    if not results:
        print("（无命中）")
        return
    for r in results:
        print(f"  {r['score']:.3f}  [{','.join(r['sources'])}]  {r['id']}  ({r['file']})")
    print(f"共 {len(results)} 条（{note}）")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codegraph", description="自研代码图谱：解析 Python 仓库为调用图，分析改动爆炸半径")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="解析仓库并建图入库（有快照自动增量）")
    a.add_argument("repo", help="仓库根目录")
    a.add_argument("--db", default="codegraph.db", help="KuzuDB 库路径")
    a.add_argument("--force", action="store_true", help="忽略快照，强制全量重建")

    i = sub.add_parser("impact", help="某符号的爆炸半径")
    i.add_argument("symbol")
    i.add_argument("--db", default="codegraph.db")
    i.add_argument("--depth", type=int, default=2)

    for name in ("callers", "callees"):
        c = sub.add_parser(name, help=f"某符号的{name}")
        c.add_argument("symbol")
        c.add_argument("--db", default="codegraph.db")
        c.add_argument("--depth", type=int, default=2)

    s = sub.add_parser("search", help="语义检索：按关键词/意思找代码符号（BM25，向量后端可插拔）")
    s.add_argument("query", help="检索词，如 \"login\" 或 \"做登录鉴权的函数\"")
    s.add_argument("--db", default="codegraph.db")
    s.add_argument("--repo", required=True, help="仓库根目录（取代码片段建索引）")
    s.add_argument("--k", type=int, default=10)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.cmd == "analyze":
        _analyze(args.repo, args.db, force=args.force)
    elif args.cmd == "impact":
        _impact(args.db, args.symbol, args.depth)
    elif args.cmd in ("callers", "callees"):
        _callers_callees(args.db, args.symbol, args.depth, args.cmd)
    elif args.cmd == "search":
        _search(args.db, args.repo, args.query, args.k)


if __name__ == "__main__":
    main()
