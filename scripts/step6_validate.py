"""Step6 业务工程级增量验证。

模拟一个多模块电商后端（包 + barrel 重导出 + 跨模块调用链），多轮业务演进逐一做增量写回，
以「增量累积库 与 全量重建库 完全等价」为黄金正确性标准（无论解析器局限如何，增量必须复现全量），
并断言 importer 传播、未变文件不重写、删除清边。

场景（业务演进轮次）：
  r0 基线：cart / order / payment / user / main + barrel(__init__)
  r1 改核心 cart.py          → 断言 importer 沿  order→payment→main 传播
  r2 新增 promo.py + 改 order.py
  r3 改 barrel __init__.py    → 断言引用 barrel 的模块被重解析
  r4 删除 payment.py          → 断言悬空调用边清理
  r5 多轮交错：改 user + 新增 review + 改 main
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from codegraph import db  # noqa: E402
from codegraph.incremental import run_incremental  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="biz_repo_"))
INC_DB = ROOT / "inc.db"
INC_SN = f"{INC_DB}.snapshot.json"
FULL_DB = ROOT / "full.db"

# ---------------- 每轮完整文件集 ----------------
ROUNDS: list[dict[str, str]] = [
    {  # r0 基线
        "shop/__init__.py": "from .cart import Cart, add_item\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return cart.Cart()\n"
        ),
        "shop/payment.py": (
            "from .order import create_order\n"
            "def pay(c):\n"
            "    return create_order(c)\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/main.py": (
            "from .payment import pay\n"
            "from . import user\n"
            "def run_app():\n"
            "    pay(None)\n"
            "    return user.get_user()\n"
        ),
    },
    {  # r1 改核心 cart.py：加 total2、改 add_item
        "shop/__init__.py": "from .cart import Cart, add_item\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return cart.Cart()\n"
        ),
        "shop/payment.py": (
            "from .order import create_order\n"
            "def pay(c):\n"
            "    return create_order(c)\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/main.py": (
            "from .payment import pay\n"
            "from . import user\n"
            "def run_app():\n"
            "    pay(None)\n"
            "    return user.get_user()\n"
        ),
    },
    {  # r2 新增 promo.py，order.py 改用它
        "shop/__init__.py": "from .cart import Cart, add_item\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "from .promo import discount\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return discount(cart.Cart())\n"
        ),
        "shop/promo.py": (
            "def discount(cart, pct=0.9):\n"
            "    return cart.total() * pct\n"
        ),
        "shop/payment.py": (
            "from .order import create_order\n"
            "def pay(c):\n"
            "    return create_order(c)\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/main.py": (
            "from .payment import pay\n"
            "from . import user\n"
            "def run_app():\n"
            "    pay(None)\n"
            "    return user.get_user()\n"
        ),
    },
    {  # r3 改 barrel __init__.py：重导出集合变化
        "shop/__init__.py": "from .cart import Cart, add_item, total2\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "from .promo import discount\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return discount(cart.Cart())\n"
        ),
        "shop/promo.py": (
            "def discount(cart, pct=0.9):\n"
            "    return cart.total() * pct\n"
        ),
        "shop/payment.py": (
            "from .order import create_order\n"
            "def pay(c):\n"
            "    return create_order(c)\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/main.py": (
            "from . import cart\n"
            "from . import user\n"
            "def run_app():\n"
            "    cart.add_item(None, 1)\n"
            "    return user.get_user()\n"
        ),
    },
    {  # r4 删除 payment.py
        "shop/__init__.py": "from .cart import Cart, add_item, total2\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "from .promo import discount\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return discount(cart.Cart())\n"
        ),
        "shop/promo.py": (
            "def discount(cart, pct=0.9):\n"
            "    return cart.total() * pct\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name):\n"
            "        self.name = name\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/main.py": (
            "from . import cart\n"
            "from . import user\n"
            "def run_app():\n"
            "    cart.add_item(None, 1)\n"
            "    return user.get_user()\n"
        ),
    },
    {  # r5 多轮交错：改 user.py + 新增 review.py + 改 main.py
        "shop/__init__.py": "from .cart import Cart, add_item, total2\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "from .promo import discount\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return discount(cart.Cart())\n"
        ),
        "shop/promo.py": (
            "def discount(cart, pct=0.9):\n"
            "    return cart.total() * pct\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name, role='user'):\n"
            "        self.name = name\n"
            "        self.role = role\n"
            "    def is_admin(self):\n"
            "        return self.role == 'admin'\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/review.py": (
            "from .user import get_user\n"
            "def review():\n"
            "    return get_user()\n"
        ),
        "shop/main.py": (
            "from . import cart\n"
            "from .review import review\n"
            "def run_app():\n"
            "    cart.add_item(None, 1)\n"
            "    return review()\n"
        ),
    },
    {  # r6：删除被引用文件 promo.py（order import promo）→ order 应被重解析
        "shop/__init__.py": "from .cart import Cart, add_item, total2\n",
        "shop/cart.py": (
            "class Cart:\n"
            "    def __init__(self):\n"
            "        self.items = []\n"
            "    def total(self):\n"
            "        return sum(self.items)\n"
            "    def total2(self):\n"
            "        return sum(self.items) * 2\n"
            "def add_item(cart, item):\n"
            "    cart.items.append(item)\n"
            "    return cart.total2()\n"
        ),
        "shop/order.py": (
            "from . import cart\n"
            "def create_order(c):\n"
            "    cart.add_item(c, 10)\n"
            "    return cart.Cart()\n"
        ),
        "shop/user.py": (
            "class User:\n"
            "    def __init__(self, name, role='user'):\n"
            "        self.name = name\n"
            "        self.role = role\n"
            "    def is_admin(self):\n"
            "        return self.role == 'admin'\n"
            "def get_user():\n"
            "    return User('a')\n"
        ),
        "shop/review.py": (
            "from .user import get_user\n"
            "def review():\n"
            "    return get_user()\n"
        ),
        "shop/main.py": (
            "from . import cart\n"
            "from .review import review\n"
            "def run_app():\n"
            "    cart.add_item(None, 1)\n"
            "    return review()\n"
        ),
    },
]

ROUND_CHECKS: list[dict] = [
    {},  # r0 基线
    {  # r1：改核心，importer 传播
        "expect_reparsed_has": ["shop/cart.py", "shop/order.py", "shop/payment.py", "shop/main.py", "shop/__init__.py"],
        "expect_reparsed_absent": ["shop/user.py"],
    },
    {  # r2：新增 promo + 改 order
        "expect_added": ["shop/promo.py"],
        "expect_reparsed_has": ["shop/promo.py", "shop/order.py"],
    },
    {  # r3：改 barrel
        "expect_reparsed_has": ["shop/__init__.py", "shop/main.py"],
    },
    {  # r4：删除孤立文件 payment.py（r3 改版后已无 importer）→ 只删不重插
        "expect_deleted": ["shop/payment.py"],
    },
    {  # r5：多轮交错
        "expect_added": ["shop/review.py"],
        "expect_reparsed_has": ["shop/user.py", "shop/review.py", "shop/main.py"],
        "expect_reparsed_absent": ["shop/cart.py", "shop/order.py", "shop/promo.py"],
    },
    {  # r6：删除被引用文件 promo.py → importer order 被重解析
        "expect_deleted": ["shop/promo.py"],
        "expect_reparsed_has": ["shop/order.py"],
    },
]


def w(rel: str, content: str) -> None:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def apply_round(idx: int) -> None:
    files = ROUNDS[idx]
    prev = ROUNDS[idx - 1] if idx > 0 else {}
    for f in prev:
        if f not in files:
            (ROOT / f).unlink()
    for rel, content in files.items():
        w(rel, content)


def full_rebuild() -> None:
    from codegraph.cli import _analyze

    db.wipe_db(FULL_DB)
    for sfx in (".snapshot.json",):
        p = Path(f"{FULL_DB}{sfx}")
        if p.exists():
            p.unlink()
    _analyze(str(ROOT), str(FULL_DB), force=True)


def _count(conn, q: str) -> int:
    r = conn.execute(q)
    return next(iter(r))[0]


def graph_sig(db_path: Path) -> tuple[dict, dict, list]:
    conn = db.connect(db_path)
    try:
        node = {t: _count(conn, f"MATCH (n:{t}) RETURN count(*)") for t in ("File", "Function", "Class")}
        # 注意：kuzu 的 count(*) 对经历 DETACH DELETE+重插的表统计可能滞后（此前实测
        # 同一库 count(*)=44 而数行 43；此处 count(*)=8 而数行 7）。关系表一律用扫描数行。
        rel = {
            t: sum(1 for _ in conn.execute(f"MATCH (a)-[r:{t}]->(b) RETURN a.id,b.id"))
            for t, *_ in db.REL_TABLES
        }
        syms = sorted(
            r[0]
            for t in ("Function", "Class")
            for r in conn.execute(f"MATCH (n:{t}) RETURN n.id AS id")
        )
        files = sorted(r[0] for r in conn.execute("MATCH (n:File) RETURN n.id AS id"))
    finally:
        conn.close()
    return node, rel, syms, files


def assert_equivalent() -> None:
    a = graph_sig(INC_DB)
    b = graph_sig(FULL_DB)
    if a != b:
        # 失败时打印边级差集，便于定位重复/缺失的具体边
        def _edges(p: Path, rel: str) -> set:
            c = db.connect(p)
            s = {tuple(r) for r in c.execute(f"MATCH (a)-[r:{rel}]->(b) RETURN a.id,b.id")}
            c.close()
            return s
        diffs = []
        for t, *_ in db.REL_TABLES:
            ei, ef = _edges(INC_DB, t), _edges(FULL_DB, t)
            if ei != ef:
                diffs.append(f"{t}: inc-only={sorted(ei - ef)[:6]} ful-only={sorted(ef - ei)[:6]}")
        # 原始行（含重复 rel）诊断
        c = db.connect(INC_DB)
        raw = [tuple(r) for r in c.execute("MATCH (a)-[r:R_CALLS_FF]->(b) RETURN a.id,b.id")]
        c.close()
        from collections import Counter
        raw_dup = {k: n for k, n in Counter(raw).items() if n > 1}
        raise AssertionError(
            f"增量与全量不等价！\n  增量节点/边: {a[0]} / {a[1]}\n  全量节点/边: {b[0]} / {b[1]}\n"
            f"  增量符号差集: {set(a[2]) ^ set(b[2])}\n  增量文件差集: {set(a[3]) ^ set(b[3])}\n"
            f"  边级差集: {diffs}\n  增量 R_CALLS_FF 原始行重复: {raw_dup}"
        )


def main() -> None:
    print(f"业务仓库：{ROOT}")
    for i in range(len(ROUNDS)):
        apply_round(i)
        if i == 0:
            full_rebuild()
            st = run_incremental(INC_DB, ROOT, INC_SN)
            assert st["mode"] == "full-needed", st  # 无快照先走全量（由 cli 处理），这里直接全量建
            from codegraph.cli import _analyze

            _analyze(str(ROOT), str(INC_DB), force=True)
            assert_equivalent()
            print(f"✅ r{i} 基线建图：增量库 == 全量库")
            continue

        st = run_incremental(INC_DB, ROOT, INC_SN)
        full_rebuild()
        checks = ROUND_CHECKS[i]
        assert st["mode"] == "incremental", st
        if checks.get("expect_added"):
            for f in checks["expect_added"]:
                assert f in st["added_files"], f"r{i} 期望新增 {f} 实际 {st['added_files']}"
        if checks.get("expect_deleted"):
            for f in checks["expect_deleted"]:
                assert f in st["deleted_files"], f"r{i} 期望删除 {f} 实际 {st['deleted_files']}"
        if checks.get("expect_reparsed_has"):
            for f in checks["expect_reparsed_has"]:
                assert f in st["reparsed_files"], f"r{i} 期望重解析含 {f} 实际 {st['reparsed_files']}"
        if checks.get("expect_reparsed_absent"):
            for f in checks["expect_reparsed_absent"]:
                assert f not in st["reparsed_files"], f"r{i} 期望未重解析 {f} 实际 {st['reparsed_files']}"
        assert_equivalent()
        print(
            f"✅ r{i} 增量：reparsed={st['reparsed_files']} | "
            f"add={st['added_files']} del={st['deleted_files']} | 增量库 == 全量库"
        )

    print("\n✅ Step6 业务工程级增量验证通过（多轮演进 × 增量≡全量等价）")
    shutil.rmtree(ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
