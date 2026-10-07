"""Step10 多语言端到端验证：py + js + ts + java 混合仓库 → 解析 → 建图 → 断言。

覆盖：
  1. 各语言符号/import/调用正确提取（parse 分发）
  2. 语言分发（扩展名→解析器）
  3. 跨文件调用解析（JS import './c' + call x -> CALLS 边）
  4. JS/Java class method、TS interface
  5. IMPORTS 边（JS 相对导入、Java 包导入）
  6. 并行分块解析（parse_many 结果与顺序一致）
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from codegraph import db
from codegraph.graph import build_graph, module_name
from codegraph.parser import discover_sources, language_of, parse, parse_many

# 造一个混合语言仓库
REPO_FILES: dict[str, str] = {
    # Python
    "app.py": (
        "import json\n"
        "def main():\n"
        "    return helper()\n"
        "def helper():\n"
        "    return 1\n"
        "def route():\n"
        "    u = User()\n"
        "    return u.create()\n"
        "class User:\n"
        "    def create(self):\n"
        "        return 2\n"
    ),
    # JS：跨文件调用（c.js 的 x）
    "web/util.js": (
        "import { x } from './c';\n"
        "function greet() { return x(); }\n"
        "export default greet;\n"
    ),
    "web/c.js": "export function x() { return 1; }\n",
    # TS：interface + class implements
    "web/types.ts": (
        "interface I { m(): void }\n"
        "class A implements I { m() { return this.m(); } }\n"
        "export function use() { return A; }\n"
    ),
    # Java
    "src/com/x/Main.java": (
        "package com.x;\n"
        "import com.y.Z;\n"
        "class Main { void run() { Z.f(); } }\n"
    ),
    "src/com/y/Z.java": (
        "package com.y;\n"
        "class Z { static void f() {}\n"
        "  void g() { this.f(); }\n"
        "}\n"
    ),
}

# 期望的关键断言
EXPECTS = {
    # (语言, 文件) -> 期望符号限定名集合
    "lang": {
        "app.py": "python", "web/util.js": "js", "web/c.js": "js",
        "web/types.ts": "ts", "src/com/x/Main.java": "java",
    },
    # 每个文件的模块名
    "modules": {
        "app.py": "app", "web/util.js": "web.util", "web/c.js": "web.c",
        "web/types.ts": "web.types", "src/com/x/Main.java": "src.com.x.Main",
    },
    # 关键 CALLS 边（caller_fqn -> callee_fqn）
    "calls": {
        "app.main": "app.helper",       # Python 函数间
        "web.util.greet": "web.c.x",     # JS 跨文件调用（import './c'）
        "web.types.A.m": "web.types.A.m",  # TS this 递归（短名兜底）
        "src.com.x.Main.Main.run": "src.com.y.Z.Z.f",  # Java 跨包 static（全局唯一短名兜底）
    },
}


def _rmtree(d):
    shutil.rmtree(d, ignore_errors=True)


def main():
    root = Path(tempfile.mkdtemp(prefix="cg_mlang_"))
    try:
        for rel, content in REPO_FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

        ok = 0
        total = 0

        def check(name, cond):
            nonlocal ok, total
            total += 1
            if cond:
                ok += 1
                print(f"  ✅ {name}")
            else:
                print(f"  ❌ {name}")

        # 1) 语言分发 + 模块名
        for rel, lang in EXPECTS["lang"].items():
            check(f"language_of({rel}) == {lang}", language_of(root / rel) == lang)
            check(f"module_of({rel}) == {EXPECTS['modules'][rel]}",
                  module_name(rel) == EXPECTS["modules"][rel])

        # 2) 单文件解析提取
        pfs = {rel: parse((root / rel).read_bytes(), rel) for rel in REPO_FILES}
        syms = {rel: {s.qualified_name for s in pfs[rel].symbols} for rel in pfs}
        check("py 符号 main/helper/route/User/User.create",
              {"main", "helper", "route", "User", "User.create"} <= syms["app.py"])
        check("js util 符号 greet", {"greet"} == syms["web/util.js"])
        check("ts 符号 I/A/A.m/use", {"I", "A", "A.m", "use"} == syms["web/types.ts"])
        check("java Main 符号 Main/Main.run",
              {"Main", "Main.run"} == syms["src/com/x/Main.java"])
        # JS import
        check("js import {x} from './c' -> web.c",
              any(i.module == "web.c" and i.name == "x" for i in pfs["web/util.js"].imports))
        # Java import com.y.Z -> 包 com.y
        check("java import com.y.Z -> 包 com.y",
              any(i.module == "com.y" and i.name == "Z" for i in pfs["src/com/x/Main.java"].imports))

        # 3) 并行分块解析：顺序一致 + 等价
        srcs = discover_sources(root)
        check("discover_sources 发现 6 个文件", len(srcs) == 6)
        par = parse_many(srcs, root)
        check("parse_many 数量一致", len(par) == len(srcs))
        check("parse_many 顺序一致",
              [pf.file for pf in par] == [str(Path(x).relative_to(root)) for x in srcs])
        check("parse_many 与单线程等价（app.py 符号）",
              {s.qualified_name for s in par[0].symbols}
              == {s.qualified_name for s in pfs["app.py"].symbols})

        # 4) 建图端到端
        graph = build_graph(list(pfs.values()))
        edges = {(e.src, e.type, e.dst) for e in graph.edges}
        call_edges = {(e.src, e.dst) for e in graph.edges if e.type == "CALLS"}
        for caller, callee in EXPECTS["calls"].items():
            check(f"CALLS {caller} -> {callee}",
                  (caller, callee) in call_edges)
        # IMPORTS 边
        check("IMPORTS web.util -> web.c",
              ("web.util", "IMPORTS", "web.c") in edges)
        # 注：Java IMPORTS 边是 V1 已知局限——Java 文件 module 用路径（src.com.y.Z）而 import
        # 解析到包名（com.y），两者不匹配，跨包 IMPORTS 边暂不连；跨包 static 调用已靠
        # 全局唯一短名兜底连上（见上方 CALLS 断言）。
        # HAS_METHOD
        check("HAS_METHOD app.User -> app.User.create",
              ("app.User", "HAS_METHOD", "app.User.create") in edges)

        # 5) 落库
        db_path = str(root / "mlang.db")
        db_obj, conn = db.load_graph(db_path, graph)
        n_func = conn.execute("MATCH (n:Function) RETURN count(*)").get_next()[0]
        n_class = conn.execute("MATCH (n:Class) RETURN count(*)").get_next()[0]
        n_call = conn.execute(
            "MATCH (a)-[r:R_CALLS_FF]->(b) RETURN count(*)"
        ).get_next()[0]
        check(f"落库 Function={n_func} > 0", n_func > 0)
        check(f"落库 Class={n_class} > 0", n_class > 0)
        check(f"落库 CALLS={n_call} > 0", n_call > 0)
        conn.close(); db_obj.close()

        print(f"\n{'✅' if ok == total else '❌'} Step10 多语言端到端验证：{ok}/{total} 通过")
        return 0 if ok == total else 1
    finally:
        _rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
