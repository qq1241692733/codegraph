"""增量写回模块：内容哈希判定变更 → importer BFS 依赖方扩展 → 只重写受影响子图。

借鉴 GitNexus 的 hashDiff + importer 反查 + 子图写回三件套：
  1. 快照（<db>.snapshot.json）存每个文件的 sha1，对比当前算出 added/changed/deleted。
  2. 变更文件的 importers（沿 IMPORTS 边反向 BFS）也拉进重解析集——改了 X 的导出，
     import 了 X 的文件的调用解析可能变化，必须一起重算。
  3. 只对受影响子图 DETACH DELETE + MERGE 写回，未变文件的行不动。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import db
from .graph import build_graph, module_name
from .parser import discover_sources, parse_many


def sha1_files(root: Path) -> dict[str, str]:
    """扫描 root 下全部受支持语言文件（.py/.js/.ts/.tsx/.java），返回 {相对路径: sha1}。"""
    hashes: dict[str, str] = {}
    for p in discover_sources(root):
        rel = os.path.relpath(p, root).replace("\\", "/")
        with open(p, "rb") as f:
            hashes[rel] = hashlib.sha1(f.read()).hexdigest()
    return hashes


def read_snapshot(path: str | Path) -> dict[str, str] | None:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_snapshot(path: str | Path, data: dict[str, str]) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=0))


def run_incremental(
    db_path: str | Path, root: str | Path, snapshot_path: str | Path | None = None
) -> dict:
    """增量写回。返回状态：up-to-date / incremental（含统计）/ full-needed（无快照）。

    无快照时不建库（应由调用方走全量 analyze），这里只报告需要全量。
    """
    root = Path(root)
    snapshot_path = snapshot_path or f"{db_path}.snapshot.json"
    cur = sha1_files(root)
    old = read_snapshot(snapshot_path)
    if old is None:
        return {"mode": "full-needed", "reason": "no-snapshot"}

    added = {f for f in cur if f not in old}
    changed = {f for f in cur if f in old and cur[f] != old[f]}
    deleted = set(old) - set(cur)
    seed = added | changed | deleted
    if not seed:
        return {"mode": "up-to-date", "reparsed": 0, "added": 0, "changed": 0, "deleted": 0}

    db_obj, conn = db.open_with_conn(db_path)
    try:
        # 迭代写集闭包：被删除/重写文件的 importer 与"调用了其符号的文件"都必须重解析，
        # 且新纳入的文件符号也会被删，需继续找它们的引用方，直到收敛（GitNexus 的
        # computeEffectiveWriteSet 跨边界游走）。deleted 文件只在 seed 里驱动传播，不重插。
        seen_files: set[str] = set(seed)
        reparse: set[str] = set()
        to_process: set[str] = set(seed)
        while to_process:
            imp_mods = db.find_importers(conn, {module_name(f) for f in to_process})
            importer_files = {f for f in cur if module_name(f) in imp_mods}
            caller_files = db.find_callers_files(conn, to_process) & set(cur)
            new_files = importer_files | caller_files
            reparse |= (to_process & set(cur))
            to_process = new_files - seen_files
            seen_files |= new_files
        delete_set = reparse | deleted  # 库中要删的范围（deleted 只删不重插）

        new_parsed = parse_many([root / f for f in sorted(reparse)], root)
        extra_syms, extra_files = db.load_symbols_except(conn, delete_set)  # 未变文件符号 + 路径
        subgraph = build_graph(new_parsed, extra_symbols=extra_syms, extra_files=extra_files)

        db.detach_delete_for_files(conn, sorted(delete_set))
        db.upsert_nodes(conn, subgraph.nodes)
        node_label = {n.id: n.label for n in subgraph.nodes}
        node_label.update(subgraph.extra_labels)  # 边可能指向未变文件符号
        db.insert_edges(conn, subgraph.edges, node_label)
        conn.execute("CHECKPOINT")  # 确保 WAL 落盘
    finally:
        conn.close()
        db_obj.close()

    save_snapshot(snapshot_path, cur)
    return {
        "mode": "incremental",
        "reparsed": len(reparse),
        "reparsed_files": sorted(reparse),
        "importers": len(importer_files),
        "importers_files": sorted(importer_files),
        "added": len(added),
        "added_files": sorted(added),
        "changed": len(changed),
        "changed_files": sorted(changed),
        "deleted": len(deleted),
        "deleted_files": sorted(deleted),
        "unresolved": getattr(subgraph, "props_unresolved", 0),
    }
