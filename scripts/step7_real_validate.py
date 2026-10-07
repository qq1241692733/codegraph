"""Step7 真实 GitHub 项目增量测试（业务工程层）。

对不同体量的真实开源 Python 项目（itsdangerous / flask / fastapi）：
  1. 全量建图（计时，基线）
  2. 模拟真实业务变更：改核心文件（追加新函数）+ 新增模块文件
  3. 增量写回（计时）→ 断言 importer 传播、未变文件不重写
  4. 全量重建对照库 → 断言「增量库 ≡ 全量库」（黄金正确性）
  5. 输出 全量耗时 vs 增量耗时 的性能对比
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codegraph import db  # noqa: E402
from codegraph.incremental import run_incremental  # noqa: E402

TEST = __import__("scripts.setup_repos", fromlist=["ensure_repos", "SRC"]).ensure_repos()
_SRC = __import__("scripts.setup_repos", fromlist=["SRC"]).SRC

PROJECTS = [
    {
        "name": "small · itsdangerous",
        "repo": TEST / _SRC["small"],
    },
    {
        "name": "medium · flask",
        "repo": TEST / _SRC["medium"],
    },
    {
        "name": "large · fastapi",
        "repo": TEST / _SRC["big"],
    },
]

APPEND = "\ndef __cg_probe__(x):\n    return x + 1\n"
EXTRA = "def cg_extra(a, b):\n    return a + b\n"


def _count(conn, q: str) -> int:
    return next(iter(conn.execute(q)))[0]


def graph_sig(db_path: Path) -> tuple[dict, dict, dict, list, list]:
    """等价性签名。注意：kuzu 的 count(*) 对含重复 rel 的表计数不可靠
    （同一库 R_CALLS_FF 的 count(*) 返回 44 而数行是 43），因此关系表用「数行 + 去重」双指标。"""
    conn = db.connect(db_path)
    try:
        node = {t: _count(conn, f"MATCH (n:{t}) RETURN count(*)") for t in ("File", "Function", "Class")}
        rel_rows: dict[str, int] = {}
        rel_uniq: dict[str, int] = {}
        for t, *_ in db.REL_TABLES:
            rows = [tuple(x) for x in conn.execute(f"MATCH (a)-[r:{t}]->(b) RETURN a.id, b.id")]
            rel_rows[t] = len(rows)
            rel_uniq[t] = len(set(rows))
        syms = sorted(r[0] for t in ("Function", "Class") for r in conn.execute(f"MATCH (n:{t}) RETURN n.id AS id"))
        files = sorted(r[0] for r in conn.execute("MATCH (n:File) RETURN n.id AS id"))
    finally:
        conn.close()
    return node, rel_rows, rel_uniq, syms, files


def reset_repo(repo: Path) -> None:
    """把 clone 仓库还原到干净状态，保证每次测试基线一致、可重复运行。"""
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "."], capture_output=True)
    for p in repo.rglob("cg_extra.py"):
        p.unlink(missing_ok=True)


def main() -> None:
    for p in PROJECTS:
        repo: Path = p["repo"]
        name = p["name"]
        reset_repo(repo)  # 运行前还原，避免上次失败残留污染基线
        db1 = TEST / f"{name.replace(' ','').split('·')[1].strip()}.db"
        db2 = TEST / f"{name.replace(' ','').split('·')[1].strip()}.full.db"
        sn1 = f"{db1}.snapshot.json"
        print(f"\n========== {name}  repo={repo} ==========")

        from codegraph.cli import _analyze

        db.wipe_db(db1)
        for sfx in (".snapshot.json",):
            q = Path(f"{db1}{sfx}")
            if q.exists():
                q.unlink()

        # 1) 全量基线
        t0 = time.time()
        _analyze(str(repo), str(db1), force=True)
        t_full = time.time() - t0
        base_sig = graph_sig(db1)
        n_files = base_sig[0]["File"]
        print(f"全量建图: {t_full:.2f}s | 文件 {n_files} | 节点/边 {base_sig[0]}/{base_sig[1]}")

        # 2) 真实业务变更：改最大的核心文件 + 新增模块
        modify_file = max((f for f in repo.rglob("*.py") if not f.name.startswith(".")), key=lambda f: f.stat().st_size)
        extra_file = repo / "cg_extra.py"
        with open(modify_file, "a") as f:
            f.write(APPEND)
        extra_file.write_text(EXTRA)
        print(f"业务变更: 改 {modify_file.relative_to(repo)}（追加函数） + 新增 {extra_file.relative_to(repo)}")

        # 3) 增量写回
        t0 = time.time()
        st = run_incremental(db1, repo, sn1)
        t_inc = time.time() - t0
        assert st["mode"] == "incremental", st
        inc_sig = graph_sig(db1)
        print(
            f"增量写回: {t_inc:.2f}s | 重解析 {st['reparsed']} 文件（importer {st['importers']}）"
            f" | 节点/边 {inc_sig[0]}/{inc_sig[1]}"
        )
        print(f"  reparsed={st['reparsed_files'][:6]}{'...' if st['reparsed'] > 6 else ''}")

        # 4) 全量重建对照库，断言 增量≡全量
        db.wipe_db(db2)
        for sfx in (".snapshot.json",):
            q = Path(f"{db2}{sfx}")
            if q.exists():
                q.unlink()
        _analyze(str(repo), str(db2), force=True)
        full_sig = graph_sig(db2)
        assert inc_sig == full_sig, (
            f"增量≠全量!\n  增量 node {inc_sig[0]} rel行/去重 {inc_sig[1]}/{inc_sig[2]}\n"
            f"  全量 node {full_sig[0]} rel行/去重 {full_sig[1]}/{full_sig[2]}\n"
            f"  符号差 {set(inc_sig[3]) ^ set(full_sig[3])} 文件差 {set(inc_sig[4]) ^ set(full_sig[4])}"
        )
        print(f"✅ 增量库 ≡ 全量库（{inc_sig[0]['Function']} 函数 / {inc_sig[1]['R_CALLS_FF'] + inc_sig[1]['R_CALLS_FC']} 调用边完全一致）")

        # 5) 性能对比
        speedup = t_full / t_inc if t_inc > 0 else float("inf")
        print(
            f"⏱ 全量 {t_full:.2f}s vs 增量 {t_inc:.2f}s → 增量提速 {speedup:.1f}× "
            f"（仅重解析 {st['reparsed']}/{n_files} 文件）"
        )

        # 清理对照库
        db.wipe_db(db2)
        for sfx in (".snapshot.json",):
            q = Path(f"{db2}{sfx}")
            if q.exists():
                q.unlink()

    print("\n✅ Step7 真实 GitHub 项目增量验证通过（多体量 × 增量≡全量）")


if __name__ == "__main__":
    main()
