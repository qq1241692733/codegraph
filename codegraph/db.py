"""KuzuDB 存储层：混合 schema（节点表 + 强类型关系表，逻辑上等价单一 CodeRelation）。

kuzu 0.11 的 REL TABLE 是强类型的（必须 FROM 具体节点表 TO 具体节点表），不支持
GitNexus 那种 `FROM * TO *` 多态单表。因此这里按 (类型, 源, 目标) 拆成多张 REL 表，
但通过 REL_TABLES 目录 + 统一查询函数封装成"逻辑单一 CodeRelation"：
调用方无需关心底层拆表，find_callers/find_callees 自动 UNION 相关表。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import kuzu

from .graph import CALLS, CONTAINS, HAS_METHOD, IMPORTS, CodeGraph, GraphEdge, GraphNode, module_name
from .parser import Symbol


def wipe_db(db_path: str | Path) -> None:
    """彻底删除 kuzu 库（可能是单文件或目录），供全量重建前清理。

    kuzu 0.11 在本机生成**单文件**库，shutil.rmtree 对文件会报 NotADirectoryError；
    历史上也曾出现过 16KB 残留空库。因此必须兼容文件与目录两种情况，
    否则同路径 force 重建会因 catalog 残留报 "File already exists in catalog"。
    """
    p = Path(db_path)
    if p.is_dir():
        shutil.rmtree(p, ignore_errors=True)
    elif p.exists():
        p.unlink()

# (关系表名, 关系类型, 源节点表, 目标节点表)
REL_TABLES: list[tuple[str, str, str, str]] = [
    ("R_CALLS_FF", CALLS, "Function", "Function"),
    ("R_CALLS_FC", CALLS, "Function", "Class"),
    ("R_CONTAINS_FF", CONTAINS, "File", "Function"),
    ("R_CONTAINS_FC", CONTAINS, "File", "Class"),
    ("R_HAS_METHOD", HAS_METHOD, "Class", "Function"),
    ("R_IMPORTS", IMPORTS, "File", "File"),
]

NODE_TABLE_STATEMENTS = [
    "CREATE NODE TABLE File(id STRING, name STRING, path STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Function(id STRING, name STRING, file STRING, PRIMARY KEY(id))",
    "CREATE NODE TABLE Class(id STRING, name STRING, file STRING, PRIMARY KEY(id))",
]


def open_database(db_path: str | Path) -> kuzu.Database:
    return kuzu.Database(str(db_path))


def connect(db_path: str | Path) -> kuzu.Connection:
    """打开已有库的连接（只读分析用，不重建 schema）。"""
    return kuzu.Connection(open_database(db_path))


def open_with_conn(db_path: str | Path) -> tuple[kuzu.Database, kuzu.Connection]:
    """打开库并返回 (db, conn)，调用方负责关闭（用于需要 CHECKPOINT 的写操作）。"""
    db = open_database(db_path)
    return db, kuzu.Connection(db)


def find_importers(conn: kuzu.Connection, seed_modules: set[str], max_depth: int = 4) -> set[str]:
    """沿 IMPORTS 边反向 BFS：找出"直接或传递引用 seed 模块"的所有模块（依赖方扩展）。"""
    tables = _tables_of_type(IMPORTS)
    found: set[str] = set()
    seen = set(seed_modules)
    frontier = list(seed_modules)
    for _ in range(max_depth):
        if not frontier:
            break
        qs = [f"MATCH (i)-[r:{t}]->(x) WHERE x.id IN $mods RETURN DISTINCT i.id AS mid" for t in tables]
        rows = _rows(conn, " UNION ALL ".join(qs), {"mods": frontier})
        next_frontier: list[str] = []
        for row in rows:
            m = row["mid"]
            if m not in seen:
                seen.add(m)
                next_frontier.append(m)
        found.update(next_frontier)
        frontier = next_frontier
    return found


def find_callers_files(conn: kuzu.Connection, seed_files: set[str], max_depth: int = 3) -> set[str]:
    """沿 CALLS 边反向 BFS：找出"直接或传递调用 seed 文件符号"的所有文件。

    删掉 seed 文件的符号节点时，所有指向它们的 CALLS 边被级联删除；若这些边的源文件
    不在重解析集里，边会丢失。因此必须把调用者文件也拉进重解析集（GitNexus 的
    computeEffectiveWriteSet 1-hop 跨边界游走：写集 = importer ∪ 符号被引用方）。
    """
    syms: set[str] = set()
    for label in ("Function", "Class"):
        for row in _rows(
            conn,
            f"MATCH (n:{label}) WHERE n.file IN $fs RETURN n.id AS id",
            {"fs": list(seed_files)},
        ):
            syms.add(row["id"])
    call_tables = _tables_of_type(CALLS)
    found: set[str] = set()
    seen = set(syms)
    frontier = list(syms)
    for _ in range(max_depth):
        if not frontier:
            break
        qs = [
            f"MATCH (a)-[r:{t}]->(b) WHERE b.id IN $ids RETURN DISTINCT a.id AS sid, a.file AS f"
            for t in call_tables
        ]
        rows = _rows(conn, " UNION ALL ".join(qs), {"ids": frontier})
        next_frontier: list[str] = []
        for row in rows:
            if row["f"]:
                found.add(row["f"])
            sid = row["sid"]
            if sid not in seen:
                seen.add(sid)
                next_frontier.append(sid)
        frontier = next_frontier
    return found


def init_schema(conn: kuzu.Connection) -> None:
    for s in NODE_TABLE_STATEMENTS:
        conn.execute(s)
    for table, _type, src, dst in REL_TABLES:
        conn.execute(f"CREATE REL TABLE {table}(FROM {src} TO {dst}, confidence DOUBLE)")


def _rel_table_for(type_: str, src_label: str, dst_label: str) -> str | None:
    for table, t, src, dst in REL_TABLES:
        if t == type_ and src == src_label and dst == dst_label:
            return table
    return None


def _tables_of_type(type_: str) -> list[str]:
    return [t for t, typ, *_ in REL_TABLES if typ == type_]


def load_graph(db_path: str | Path, graph: CodeGraph) -> tuple[kuzu.Database, kuzu.Connection]:
    db = open_database(db_path)
    conn = kuzu.Connection(db)
    init_schema(conn)
    upsert_nodes(conn, graph.nodes)
    insert_edges(conn, graph.edges, {n.id: n.label for n in graph.nodes})
    return db, conn


def upsert_nodes(conn: kuzu.Connection, nodes: list[GraphNode]) -> None:
    """MERGE 节点：已存在则更新，不存在则建（兼容全量与增量）。

    批量优化：按 label 分组，每类节点一条 UNWIND 语句，避免逐条 execute 的往返开销
    （对齐 GitNexus 批量导入思路；实测 fastapi 432 符号从 432 次 SQL 降到 2 次）。
    """
    groups: dict[str, list[dict]] = {}
    for n in nodes:
        if n.label == "File":
            groups.setdefault("File", []).append(
                {"id": n.id, "name": n.props.get("name", ""), "path": n.props.get("path", "")}
            )
        else:
            groups.setdefault(n.label, []).append(
                {"id": n.id, "name": n.props.get("name", ""), "file": n.props.get("file", "")}
            )
    for label, rows in groups.items():
        if not rows:
            continue
        if label == "File":
            conn.execute(
                "UNWIND $rows AS r MERGE (n:File {id: r.id}) SET n.name = r.name, n.path = r.path",
                {"rows": rows},
            )
        else:
            conn.execute(
                f"UNWIND $rows AS r MERGE (n:{label} {{id: r.id}}) SET n.name = r.name, n.file = r.file",
                {"rows": rows},
            )


def insert_edges(conn: kuzu.Connection, edges: list[GraphEdge], node_label: dict[str, str]) -> None:
    """按 (类型, 源, 目标) 路由到对应 REL 表插入边。

    批量优化：按关系表分组，每张表一条 UNWIND（MATCH 源/目标后 CREATE），
    避免逐条 execute（fastapi 1191 边从 1191 次 SQL 降到 6 次）。
    """
    groups: dict[str, list[dict]] = {}
    for e in edges:
        sl, dl = node_label.get(e.src), node_label.get(e.dst)
        table = _rel_table_for(e.type, sl, dl) if sl and dl else None
        if table is None:
            continue  # 未知节点对：跳过（不应发生）
        groups.setdefault(table, []).append({"src": e.src, "dst": e.dst, "c": e.confidence})
    for table, rows in groups.items():
        if not rows:
            continue
        sl = dst = None
        for t, _typ, s, d in REL_TABLES:
            if t == table:
                sl, dst = s, d
                break
        conn.execute(
            f"UNWIND $rows AS r MATCH (a:{sl}) WHERE a.id = r.src "
            f"MATCH (b:{dst}) WHERE b.id = r.dst "
            f"CREATE (a)-[:{table} {{confidence: r.c}}]->(b)",
            {"rows": rows},
        )


def detach_delete_for_files(conn: kuzu.Connection, files: list[str]) -> None:
    """级联删除属于这些文件的全部节点与相连边。

    File 节点表没有 file 属性（只有 id/name/path），而 Function/Class 有 file，
    因此必须分别按 path / file 删，否则 File 节点（及其中间 IMPORTS/CONTAINS 边）会残留，
    导致增量写回时 IMPORTS 边重复。
    """
    for label in ("Function", "Class"):
        conn.execute(f"MATCH (n:{label}) WHERE n.file IN $fs DETACH DELETE n", {"fs": files})
    conn.execute("MATCH (n:File) WHERE n.path IN $fs DETACH DELETE n", {"fs": files})


def load_symbols_except(conn: kuzu.Connection, exclude_files: set[str]) -> tuple[list[Symbol], list[str]]:
    """读未变文件的符号与路径（排除要重写的文件），用于增量调用解析索引与 IMPORTS 边生成。

    返回 (symbols, files)：files 覆盖**无符号**的未变文件（纯 import 中转模块），
    否则增量时 reparse 文件指向它们的 IMPORTS 边会因 file_of_module 缺失而丢失。
    """
    syms: list[Symbol] = []
    files: list[str] = [
        row["path"]
        for row in _rows(conn, "MATCH (n:File) RETURN n.path AS path", {})
        if row["path"] not in exclude_files
    ]
    for label in ("Function", "Class"):
        rows = _rows(conn, f"MATCH (n:{label}) RETURN n.id AS id, n.name AS name, n.file AS file", {})
        for row in rows:
            file = row["file"]
            if file in exclude_files:
                continue
            mod = module_name(file)
            fqn_id = row["id"]
            qual = fqn_id[len(mod) + 1:] if fqn_id.startswith(mod + ".") else row["name"]
            if label == "Class":
                syms.append(Symbol(row["name"], "class", qual, file, 0, 0, None))
            else:
                kind = "method" if "." in qual else "function"
                parent = qual.rsplit(".", 1)[0] if kind == "method" else None
                syms.append(Symbol(row["name"], kind, qual, file, 0, 0, parent))
    return syms, files


def find_callers(conn: kuzu.Connection, symbol_id: str, depth: int = 1) -> list[dict]:
    """上游：谁直接调用 symbol_id（逐层 BFS，按深度分组）。"""
    results: list[dict] = []
    seen = {symbol_id}
    frontier = [symbol_id]
    for d in range(depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for f in frontier:
            for row in _query_callers_one(conn, f):
                src = row["src"]
                if src in seen:
                    continue
                seen.add(src)
                next_frontier.append(src)
                results.append({"caller": src, "callee": f, "depth": d + 1, "confidence": row["conf"]})
        frontier = next_frontier
    return results


def find_callees(conn: kuzu.Connection, symbol_id: str, depth: int = 1) -> list[dict]:
    """下游：symbol_id 直接调用谁（逐层 BFS，按深度分组）。"""
    results: list[dict] = []
    seen = {symbol_id}
    frontier = [symbol_id]
    for d in range(depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for f in frontier:
            for row in _query_callees_one(conn, f):
                dst = row["dst"]
                if dst in seen:
                    continue
                seen.add(dst)
                next_frontier.append(dst)
                results.append({"caller": f, "callee": dst, "depth": d + 1, "confidence": row["conf"]})
        frontier = next_frontier
    return results


def search_symbols(conn: kuzu.Connection, fragment: str, limit: int = 20) -> list[dict]:
    """按名字片段搜索符号节点（返回 FQN + 类型）。"""
    return _rows(
        conn,
        "MATCH (n) WHERE n.id CONTAINS $f RETURN n.id AS id, label(n) AS label LIMIT $l",
        {"f": fragment, "l": limit},
    )


def _rows(conn: kuzu.Connection, query: str, params: dict) -> list[dict]:
    """把 QueryResult 转成 list[dict]，避免依赖 pyarrow。"""
    res = conn.execute(query, params)
    cols = res.get_column_names()
    return [dict(zip(cols, row)) for row in res]


def _query_callers_one(conn: kuzu.Connection, symbol_id: str) -> list[dict]:
    qs = [f"MATCH (c)-[r:{t}]->(x) WHERE x.id=$id RETURN c.id AS src, r.confidence AS conf"
          for t in _tables_of_type(CALLS)]
    return _rows(conn, " UNION ALL ".join(qs), {"id": symbol_id})


def _query_callees_one(conn: kuzu.Connection, symbol_id: str) -> list[dict]:
    qs = [f"MATCH (s)-[r:{t}]->(x) WHERE s.id=$id RETURN x.id AS dst, r.confidence AS conf"
          for t in _tables_of_type(CALLS)]
    return _rows(conn, " UNION ALL ".join(qs), {"id": symbol_id})
