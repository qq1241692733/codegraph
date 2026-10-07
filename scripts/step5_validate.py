"""Step5 增量写回验证：
全量建图 → 改文件增量（断言 importer 被扩展进重解析集）→ 再跑 up-to-date → 删文件增量（断言悬空边被清理）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codegraph import db  # noqa: E402
from codegraph.incremental import run_incremental, save_snapshot, sha1_files  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="incr_repo_"))
DB = ROOT / "g.db"
SN = f"{DB}.snapshot.json"


def w(rel: str, content: str) -> None:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def full() -> None:
    from codegraph.cli import _analyze

    _analyze(str(ROOT), str(DB), force=True)


def callees(sym: str) -> list[str]:
    conn = db.connect(DB)
    r = db.find_callees(conn, sym, 5)
    conn.close()
    return [x["callee"] for x in r]


def callers(sym: str) -> list[str]:
    conn = db.connect(DB)
    r = db.find_callers(conn, sym, 5)
    conn.close()
    return [x["caller"] for x in r]


def count_nodes() -> int:
    conn = db.connect(DB)
    r = conn.execute("MATCH (n) RETURN count(*) AS c")
    n = next(iter(r))[0]
    conn.close()
    return n


def main() -> None:
    # 三文件：b import a，c import b（制造 importer 链）
    w("a.py", "def f():\n    return 1\n")
    w("b.py", "from a import f\ndef g():\n    return f()\n")
    w("c.py", "import b\ndef h():\n    return b.g()\n")

    full()
    assert callees("b.g") == ["a.f"], callees("b.g")
    assert callers("b.g") == ["c.h"], callers("b.g")
    n0 = count_nodes()
    print(f"✅ 基线 OK：b.g 下游 {callees('b.g')}，上游 {callers('b.g')}，节点 {n0}")

    # 1) 改 b.py：b.g 内部新增一个不存在的调用 f2（制造新符号 g2 + unresolved）
    w("b.py", "from a import f\ndef g():\n    return f()\ndef g2():\n    return f2()\n")
    st = run_incremental(DB, ROOT, SN)
    assert st["mode"] == "incremental", st
    assert "b.py" in st["reparsed_files"]
    assert "c.py" in st["reparsed_files"], f"importer c 应被扩展进重解析集：{st['reparsed_files']}"
    assert "a.py" not in st["reparsed_files"], "未变且非 importer 的 a.py 不应被重解析"
    assert callees("b.g") == ["a.f"], callees("b.g")  # 未变调用保持
    assert callers("b.g") == ["c.h"], callers("b.g")
    print(f"✅ 修改 b.py 增量：reparsed={st['reparsed_files']}（importer c 被扩展），新增 g2，unresolved={st['unresolved']}")

    # 2) 无变更再跑 → up-to-date
    st2 = run_incremental(DB, ROOT, SN)
    assert st2["mode"] == "up-to-date", st2
    print("✅ 无变更增量：up-to-date（跳过写回）")

    # 3) 删 b.py → b.g/b.g2 及其边、c.h->b.g 边应被清理
    (ROOT / "b.py").unlink()
    st3 = run_incremental(DB, ROOT, SN)
    assert st3["mode"] == "incremental", st3
    assert "b.py" in st3["deleted_files"]
    assert callees("b.g") == [], callees("b.g")  # b.g 已不存在
    assert callers("b.g") == [], callers("b.g")
    n1 = count_nodes()
    assert n1 < n0, f"删除后节点应减少：{n0} -> {n1}"
    print(f"✅ 删除 b.py 增量：deleted={st3['deleted_files']}，节点 {n0}->{n1}，悬空调用边已清理")

    # 清理临时库
    db_obj_path = Path(DB)
    if db_obj_path.exists():
        import shutil

        shutil.rmtree(db_obj_path, ignore_errors=True)
    print("\n✅ Step5 增量写回验证通过")


if __name__ == "__main__":
    main()
