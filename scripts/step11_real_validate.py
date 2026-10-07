"""Step11 真实多语言项目验证：zod(TS) + gson(Java) → 全量 analyze → 抽查符号 + 并行加速。

覆盖：
  1. 真实 TS 项目（zod，517 文件）全量建图 + 知名类方法入库
  2. 真实 Java 项目（gson 主模块，86 文件）全量建图 + 知名类方法入库
  3. 多线程分块解析加速（zod 并行 vs 串行）
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

from codegraph import db
from codegraph.parser import discover_sources, parse, parse_many

ROOT = Path(__file__).resolve().parent.parent / ".test_repos"
ZOD = ROOT / "zod"
GSON = ROOT / "gson" / "gson" / "src" / "main" / "java"


def _ensure_clone(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", "-q", url, str(dest)], check=True)


def main():
    _ensure_clone("https://github.com/colinhacks/zod", ZOD)
    _ensure_clone("https://github.com/google/gson", GSON)

    ok = 0
    total = 0

    def check(name, cond):
        nonlocal ok, total
        total += 1
        print(("  ✅ " if cond else "  ❌ ") + name)
        if cond:
            ok += 1

    # ---------- TS：zod 全量建图 ----------
    zod_db = ROOT / "zod.db"
    if not zod_db.exists():
        subprocess.run(["uv", "run", "python", "-m", "codegraph.cli", "analyze",
                        str(ZOD), "--db", str(zod_db)], check=True, cwd=ROOT.parent)
    c = db.connect(zod_db)
    zod_syms = {r[0] for r in c.execute("MATCH (n:Function) RETURN n.id AS id").get_all()}
    zod_syms |= {r[0] for r in c.execute("MATCH (n:Class) RETURN n.id AS id").get_all()}
    c.close()
    check("zod 入库 ZodString.email", any(s.endswith("ZodString.email") for s in zod_syms))
    check("zod 入库 ZodString.url", any(s.endswith("ZodString.url") for s in zod_syms))
    check("zod 入库 ZodSchema/parse 相关方法", any(".parse" in s for s in zod_syms))

    # ---------- Java：gson 全量建图 ----------
    gson_db = ROOT / "gson.db"
    if not gson_db.exists():
        subprocess.run(["uv", "run", "python", "-m", "codegraph.cli", "analyze",
                        str(GSON), "--db", str(gson_db)], check=True, cwd=ROOT.parent)
    c = db.connect(gson_db)
    gson_syms = {r[0] for r in c.execute("MATCH (n:Function) RETURN n.id AS id").get_all()}
    gson_syms |= {r[0] for r in c.execute("MATCH (n:Class) RETURN n.id AS id").get_all()}
    c.close()
    # 注：Java 模块命名 V1 用文件路径（含文件名）而非源码 package，故类 FQN 为
    # com.google.gson.Gson.Gson（module=文件 + 类名）。改用 package 需将 package 持久化到库
    # 并同步增量索引，属后续优化；当前跨包调用已可靠连上、检索可用。
    check("gson 入库 Gson 类（路径 module V1）",
          any(s == "com.google.gson.Gson.Gson" for s in gson_syms))
    check("gson 入库 Gson.fromJson", any(s.endswith("Gson.fromJson") for s in gson_syms))
    check("gson 入库 JsonParser.parseString",
          any(s.endswith("JsonParser.parseString") for s in gson_syms))

    # ---------- 多线程分块解析加速（zod） ----------
    srcs = discover_sources(ZOD)
    t0 = time.time()
    for f in srcs:
        parse(open(f, "rb").read(), str(Path(f).relative_to(ZOD)))
    ser = time.time() - t0
    t0 = time.time()
    parse_many(srcs, ZOD)
    par = time.time() - t0
    speedup = ser / par if par > 0 else 0
    print(f"  ⏱ zod 解析：串行 {ser:.2f}s → 并行 {par:.2f}s → 加速 {speedup:.2f}x")
    check(f"zod 并行解析加速 ≥1.3x（实测 {speedup:.2f}x）", speedup >= 1.3)

    print(f"\n{'✅' if ok == total else '❌'} Step11 真实多语言项目验证：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
