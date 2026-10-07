"""codegraph Web 服务：把代码图谱以可视化界面 + JSON API 暴露到浏览器。

复用现有查询能力（db / impact / retrieval / embedding），不新增依赖
（仅标准库 http.server）。启动：
    uv run python -m codegraph.server --db <图谱库> --repo <仓库根> [--port 8000]
界面：http://localhost:<port>/
API：
    /api/stats                   仓库概览（文件/符号/边/语言分布）
    /api/graph?limit=            图谱节点+边（供可视化）
    /api/symbols?q=&limit=       按片段查符号
    /api/impact?symbol=&depth=   爆炸半径
    /api/callers|callees?symbol=&depth=
    /api/search?q=&k=            语义检索（首次会建索引，慢）
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, embedding, retrieval
from .impact import run_impact

# 语言分布：按文件扩展名分组
_LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
}

_FRONTEND_HTML: str | None = None


def _load_html() -> str:
    """加载内置前端 index.html（与 server.py 同目录）。"""
    global _FRONTEND_HTML
    if _FRONTEND_HTML is None:
        p = Path(__file__).parent / "index.html"
        _FRONTEND_HTML = p.read_text(encoding="utf-8") if p.exists() else (
            "<html><body><h1>index.html 缺失</h1></body></html>")
    return _FRONTEND_HTML


class _App:
    def __init__(self, db_path: str, repo: str):
        self.db_path = db_path
        self.repo = Path(repo)
        self._search_index = None
        self._search_lock = threading.Lock()

    def conn(self):
        return db.connect(self.db_path)

    # ---------- 统计 ----------
    def stats(self) -> dict:
        c = self.conn()
        try:
            def cnt(q):
                return c.execute(q).get_next()[0]
            files = cnt("MATCH (n:File) RETURN count(*)")
            funcs = cnt("MATCH (n:Function) RETURN count(*)")
            classes = cnt("MATCH (n:Class) RETURN count(*)")
            edges = {}
            for t, typ, *_ in db.REL_TABLES:
                n = sum(1 for _ in c.execute(f"MATCH (a)-[r:{t}]->(b) RETURN a.id,b.id"))
                edges[typ] = edges.get(typ, 0) + n
            # 语言分布（从 File.path 扩展名）
            langs = {}
            for r in db._rows(c, "MATCH (n:File) RETURN n.path AS p", {}):
                ext = Path(r["p"] or "").suffix.lower()
                langs[_LANG_BY_EXT.get(ext, "Other")] = langs.get(_LANG_BY_EXT.get(ext, "Other"), 0) + 1
            return {"files": files, "functions": funcs, "classes": classes,
                    "edges": edges, "total_edges": sum(edges.values()), "languages": langs,
                    "db": self.db_path, "repo": str(self.repo)}
        finally:
            c.close()

    # ---------- 图谱（可视化） ----------
    # 设计：不一次全量渲染。graph(seed=None) 只返回精简种子层（File+Class，限量）；
    # 点击节点时 graph(seed=<id>) 按需返回该符号的 depth 层邻居子图，由前端动态加入。
    # 参考 Sourcegraph/Neo4j Bloom 的按需导航，避免几千节点一次渲染导致卡顿。
    def graph(self, limit: int = 200, seed: str | None = None, depth: int = 1) -> dict:
        c = self.conn()
        try:
            if seed:
                return self._expand(c, seed, depth, limit)
            # 精简种子层：只 File + Class（不渲染海量 Function），渲染量可控
            nodes, edges = [], []
            for label in ("File", "Class"):
                file_col = "n.path" if label == "File" else "n.file"
                q = (f"MATCH (n:{label}) RETURN n.id AS id, n.name AS name, "
                     f"{file_col} AS file LIMIT {limit}")
                for r in db._rows(c, q, {}):
                    nodes.append({"id": r["id"], "label": label, "name": r["name"],
                                  "file": r["file"] or ""})
            node_ids = {n["id"] for n in nodes}
            # 种子层内部边：CONTAINS(File→Class) + HAS_METHOD + IMPORTS
            for t, typ, src, dst in db.REL_TABLES:
                q = (f"MATCH (a)-[r:{t}]->(b) RETURN a.id AS s, b.id AS d "
                     f"LIMIT {limit * 3}")
                for r in db._rows(c, q, {}):
                    if r["s"] in node_ids and r["d"] in node_ids:
                        edges.append({"from": r["s"], "to": r["d"], "type": typ})
            return {"nodes": nodes, "edges": edges}
        finally:
            c.close()

    def _node_info(self, c, id_: str) -> dict | None:
        """按 id 反查节点类型与信息（File 用 path，Function/Class 用 file）。"""
        for label, file_col in (("File", "path"), ("Function", "file"), ("Class", "file")):
            q = (f"MATCH (n:{label}) WHERE n.id = $id RETURN n.id AS id, n.name AS name, "
                 f"n.{file_col} AS file")
            r = db._rows(c, q, {"id": id_})
            if r:
                return {"id": r[0]["id"], "label": label, "name": r[0]["name"],
                        "file": r[0]["file"] or ""}
        return None

    def _expand(self, c, seed: str, depth: int, limit: int) -> dict:
        """以 seed 为根，BFS 沿所有关系收集 depth 层邻居子图（按需加载）。"""
        nodes, edges = {}, {}
        frontier = {seed}
        seen = {seed}
        root = self._node_info(c, seed)
        if root:
            nodes[seed] = root
        for _ in range(max(1, depth)):
            nxt = set()
            for cur in frontier:
                for t, typ, src, dst in db.REL_TABLES:
                    for direction, key in (("out", "d"), ("in", "s")):
                        if direction == "out":
                            q = (f"MATCH (a)-[r:{t}]->(b) WHERE a.id = $s "
                                 f"RETURN b.id AS nid LIMIT {limit}")
                        else:
                            q = (f"MATCH (a)-[r:{t}]->(b) WHERE b.id = $s "
                                 f"RETURN a.id AS nid LIMIT {limit}")
                        for r in db._rows(c, q, {"s": cur}):
                            nid = r["nid"]
                            if nid not in seen:
                                info = self._node_info(c, nid)
                                if info:
                                    nodes[nid] = info
                                    nxt.add(nid)
                            edges.setdefault((cur, nid, typ), True)
                            edges.setdefault((nid, cur, typ), True)
            frontier = nxt
            seen |= nxt
            if not frontier:
                break
        return {"nodes": list(nodes.values()),
                "edges": [{"from": s, "to": d, "type": t} for (s, d, t) in edges]}

    # ---------- 符号 / 调用 ----------
    def symbols(self, q: str, limit: int = 50) -> list[dict]:
        c = self.conn()
        try:
            if q:
                return db.search_symbols(c, q, limit)
            rows = []
            for label in ("Function", "Class"):
                rows += [{"id": r["id"], "label": label}
                         for r in c.execute(f"MATCH (n:{label}) RETURN n.id AS id LIMIT {limit}")]
            return rows
        finally:
            c.close()

    def impact(self, symbol: str, depth: int):
        return run_impact(self.db_path, symbol, depth)

    def callers_callees(self, symbol: str, depth: int, kind: str):
        c = self.conn()
        try:
            fn = db.find_callers if kind == "callers" else db.find_callees
            return fn(c, symbol, depth)
        finally:
            c.close()

    # ---------- 语义检索 ----------
    def search(self, query: str, k: int) -> list[dict]:
        with self._search_lock:
            if self._search_index is None:
                c = self.conn()
                try:
                    self._search_index = retrieval.build_index(c, self.repo)
                finally:
                    c.close()
                emb = embedding.make_embedder()
                if emb:
                    embedding.attach_semantic(self._search_index, emb)
        return retrieval.hybrid_search(self._search_index, query, k)


class _Handler(BaseHTTPRequestHandler):
    app: _App = None  # type: ignore[assignment]

    def log_message(self, *a):  # 静默
        pass

    def _send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        html = _load_html()
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, relpath: str):
        """托管本地静态资源（如前端库），避免依赖外部 CDN。"""
        name = Path(relpath).name  # 防目录穿越：只用文件名
        p = Path(__file__).parent / "static" / name
        if not p.exists():
            return self._send_json({"error": "not found"}, 404)
        body = p.read_bytes()
        ctype = "application/javascript" if p.suffix == ".js" else (
            "text/css" if p.suffix == ".css" else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path, params = u.path, parse_qs(u.query)
        try:
            if path.startswith("/static/"):
                return self._send_static(path[len("/static/"):])
            if path == "/" or path == "/index.html":
                self._send_html()
                return
            if path == "/api/stats":
                return self._send_json(self.app.stats())
            if path == "/api/graph":
                return self._send_json(self.app.graph(
                    int(params.get("limit", ["200"])[0]),
                    params.get("seed", [None])[0] or None,
                    int(params.get("depth", ["1"])[0])))
            if path == "/api/symbols":
                return self._send_json(
                    self.app.symbols(params.get("q", [""])[0], int(params.get("limit", ["50"])[0])))
            if path in ("/api/impact", "/api/callers", "/api/callees"):
                symbol = params.get("symbol", [""])[0]
                depth = int(params.get("depth", ["2"])[0])
                if path == "/api/impact":
                    return self._send_json(self.app.impact(symbol, depth))
                return self._send_json(
                    self.app.callers_callees(symbol, depth, path.lstrip("/api/")))
            if path == "/api/search":
                return self._send_json(
                    self.app.search(params.get("q", [""])[0], int(params.get("k", ["10"])[0])))
            self._send_json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": str(e)}, 500)

    def do_POST(self):  # 暂不需要
        self._send_json({"error": "method not allowed"}, 405)


def serve(db_path: str, repo: str, port: int = 8000) -> None:
    app = _App(db_path, repo)
    _Handler.app = app  # 通过类属性注入，避免污染 BaseHTTPRequestHandler 构造签名
    httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"codegraph Web 界面: http://localhost:{port}/  (db={db_path}, repo={repo})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main() -> None:
    p = argparse.ArgumentParser(prog="codegraph server", description="代码图谱可视化 Web 服务")
    p.add_argument("--db", required=True, help="KuzuDB 图谱库路径")
    p.add_argument("--repo", required=True, help="仓库根目录")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    serve(args.db, args.repo, args.port)


if __name__ == "__main__":
    main()
