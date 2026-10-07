"""Step1 验证：解析示例仓库，检查符号/import/调用提取是否正确。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codegraph.parser import parse_python

REPO = Path(__file__).resolve().parent.parent / "examples" / "sample_repo"


def main() -> None:
    ok = True
    for f in sorted(REPO.glob("*.py")):
        pf = parse_python(f.read_bytes(), f.name)
        print(f"── {f.name}")
        print("  symbols:", [(s.kind, s.name, s.qualified_name) for s in pf.symbols])
        print("  imports:", [(i.module, i.name, i.alias) for i in pf.imports])
        print("  calls:  ", [(c.caller, c.callee_chain) for c in pf.calls])

    # 关键断言
    svc = parse_python((REPO / "service.py").read_bytes(), "service.py")
    names = {s.qualified_name for s in svc.symbols}
    assert "Service" in names and "Service.create_user" in names and "helper" in names, names
    imp = {(i.module, i.name) for i in svc.imports}
    assert ("auth", "login") in imp and ("models", "User") in imp, imp
    call_chains = {c.callee_chain for c in svc.calls}
    assert "login" in call_chains and "User" in call_chains and "helper" in call_chains, call_chains

    auth = parse_python((REPO / "auth.py").read_bytes(), "auth.py")
    auth_calls = {c.callee_chain for c in auth.calls}
    assert {"get_password", "verify_password"} <= auth_calls, auth_calls
    assert any(s.qualified_name == "login" and s.kind == "function" for s in auth.symbols)

    print("\n✅ parser 验证通过")
    ok = ok and True
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
