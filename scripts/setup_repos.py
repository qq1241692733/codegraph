"""确保真实测试仓库存在（缺则浅克隆），返回根目录。

仓库放到项目外的稳定路径 /home/user/Doubao/chats/38445731247826434/.test_repos/，
不依赖易被清理的 /tmp。step7/8/9 与性能脚本统一从这里取仓库。

用法：from scripts.setup_repos import TEST_ROOT, ensure_repos
"""
from __future__ import annotations

import subprocess
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
TEST_ROOT = HOME / ".test_repos"

_REPOS = {
    "small": "https://github.com/pallets/itsdangerous",
    "medium": "https://github.com/pallets/flask",
    "big": "https://github.com/fastapi/fastapi",
}

SRC = {
    "small": "small/src/itsdangerous",
    "medium": "medium/src/flask",
    "big": "big/fastapi",
}


def ensure_repos() -> Path:
    """确保三个仓库 clone 存在，缺则浅克隆。返回 TEST_ROOT。"""
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    for name, url in _REPOS.items():
        if (TEST_ROOT / name).exists():
            continue
        print(f"clone {name} <- {url} ...")
        subprocess.run(
            ["git", "clone", "--depth", "1", "-q", url, str(TEST_ROOT / name)],
            check=True,
            capture_output=True,
        )
    return TEST_ROOT


if __name__ == "__main__":
    root = ensure_repos()
    print(f"测试仓库就绪: {root}")
    for name, rel in SRC.items():
        p = root / rel
        print(f"  {name}: {p} ({'OK' if p.exists() else '缺失'})")
