"""Step2 验证：建图 + KuzuDB 落库 + 调用链查询。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _wipe(path: Path) -> None:
    """删除文件或目录（kuzu 库可能以文件或目录形式存在）。"""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codegraph.graph import build_graph
from codegraph.parser import parse_python
from codegraph import db

REPO = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"
DB_PATH = Path("/tmp") / "cg_step2"


def main() -> None:
    _wipe(DB_PATH)
    parsed = [parse_python(f.read_bytes(), str(f.relative_to(REPO))) for f in sorted(REPO.glob("*.py"))]
    graph = build_graph(parsed)

    _, conn = db.load_graph(DB_PATH, graph)

    calls = {(e.src, e.dst) for e in graph.edges if e.type == "CALLS"}
    print("CALLS 边:")
    for s, d in sorted(calls):
        print(f"   {s} -> {d}")
    print("未解析调用数:", getattr(graph, "props_unresolved", 0))

    # 断言核心调用链
    assert ("service.Service.create_user", "auth.login") in calls, calls
    assert ("auth.login", "auth.get_password") in calls, calls
    assert ("auth.login", "auth.verify_password") in calls, calls
    assert ("service.Service.create_user", "models.User") in calls, calls
    assert ("service.Service.create_user", "service.helper") in calls, calls
    assert ("main.run_app", "service.Service") in calls, calls

    # 查 auth.login 爆炸半径
    ups = db.find_callers(conn, "auth.login", depth=3)
    downs = db.find_callees(conn, "auth.login", depth=3)
    print("\n auth.login 上游:", [u["caller"] for u in ups])
    print(" auth.login 下游:", [d["callee"] for d in downs])
    assert any(u["caller"] == "service.Service.create_user" for u in ups), ups
    assert {d["callee"] for d in downs} == {"auth.get_password", "auth.verify_password"}, downs

    # 类方法归属边
    has_method = {(e.src, e.dst) for e in graph.edges if e.type == "HAS_METHOD"}
    assert ("models.User", "models.User.is_active") in has_method, has_method

    print("\n✅ Step2 建图 + KuzuDB 验证通过")


if __name__ == "__main__":
    main()
