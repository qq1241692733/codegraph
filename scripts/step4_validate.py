"""Step4 验证：tools.py 纯逻辑 + deepagents 工具注册 + agent 构造。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codegraph.tools import build_agent, context_payload, impact_payload, query_payload
from codegraph import db
from codegraph.parser import parse_python
from codegraph.graph import build_graph

DB = "/tmp/cg.db"
REPO = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"


def _build_sample_db() -> None:
    """自包含：先建 sample_repo 图到 DB，避免依赖外部残留库。"""
    files = sorted(p for p in REPO.rglob("*.py") if not p.name.startswith("."))
    parsed = [parse_python(f.read_bytes(), str(f.relative_to(REPO))) for f in files]
    graph = build_graph(parsed)
    db.wipe_db(DB)
    obj, conn = db.load_graph(DB, graph)
    conn.execute("CHECKPOINT")
    conn.close()
    obj.close()


def main() -> None:
    _build_sample_db()
    # 1) 纯逻辑工具（不依赖 deepagents，可独立测试）
    imp = impact_payload("auth.login", 2, DB)
    assert imp["upstream"]["total"] == 2, imp
    assert imp["downstream"]["total"] == 2, imp
    print("✅ impact_payload 通过")

    ctx = context_payload("auth.login", DB)
    assert "service.Service.create_user" in ctx["callers"], ctx
    print("✅ context_payload 通过")

    q = query_payload("create_user", DB)
    assert any(s["id"].endswith("create_user") for s in q["symbols"]), q
    print("✅ query_payload 通过")

    # 2) deepagents @tool 注册 + agent 构造
    from langchain_core.tools import BaseTool, tool

    @tool
    def _probe(x: str) -> str:
        """probe tool"""
        return x

    assert isinstance(_probe, BaseTool), type(_probe)
    print("✅ deepagents @tool 返回 BaseTool")

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    model = FakeListChatModel(responses=["ok"])
    agent = build_agent(model=model, db_path=DB)
    print(f"✅ build_agent 构造通过：{type(agent).__name__}")

    print("\n✅ Step4 Deep Agents 工具层验证通过")


if __name__ == "__main__":
    main()
