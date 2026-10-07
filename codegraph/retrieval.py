"""M4 语义检索引擎：BM25 关键词 + 可插拔语义向量 + RRF 融合（GraphRAG 轻量版）。

- 检索引擎本身**无大模型**：BM25 是纯算法，语义向量走本地 embedding（可插拔）。
- 文档 = 每个 Function/Class 的「函数名 + def 签名行 + body 前几行（含 docstring）」。
- `semantic_search` 默认返回空（未接向量后端）；接上后 `hybrid_search` 用 RRF 融合。
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from codegraph import db as dbmod
from codegraph.parser import parse_python
from codegraph.graph import module_name

# 常见英文停用词（足够小，覆盖大部分噪音即可）
_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "as",
    "is", "are", "be", "by", "at", "if", "else", "from", "import", "return", "def",
    "class", "self", "it", "this", "that", "we", "you", "i", "not", "no", "yes",
    "do", "does", "did", "have", "has", "had", "will", "would", "can", "could",
    "use", "get", "set", "make", "new", "using", "used", "into", "out", "up",
}
_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{1,}")

Doc = dict[str, Any]  # {"id","name","file","text","lines"}


def _tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOP and len(t) > 1]


def _snippet(lines: list[str], start: int, end: int, limit: int = 8) -> tuple[str, int]:
    """取符号的代码片段文本（def 签名行 + body 前几行）。start/end 为 1 基行号。"""
    lo = max(1, start)
    hi = min(end, start + limit)
    seg = lines[lo - 1 : hi]
    return "\n".join(seg), hi - lo + 1


class BM25Index:
    """轻量 BM25 关键词索引（k1=1.5, b=0.75）。文档 id 用符号 fqn。"""

    def __init__(self, docs: list[Doc]):
        self.docs = docs
        self.doc_id: list[str] = []
        self.doc_len: list[int] = []
        self.avgdl = 0.0
        self._tfs: list[dict[str, int]] = []
        self._df: dict[str, int] = {}
        n = len(docs)
        for d in docs:
            toks = _tokenize(d["text"])
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            self._tfs.append(tf)
            self.doc_len.append(len(toks))
            self.doc_id.append(d["id"])
            for t in set(tf):
                self._df[t] = self._df.get(t, 0) + 1
        self.avgdl = (sum(self.doc_len) / n) if n else 0.0
        self._n = n

    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]:
        """返回 [(doc_id, score), ...] 降序。query 词先做短名加成：命中函数名/文件名的词加分。"""
        toks = _tokenize(query)
        if not toks:
            return []
        idf = {
            t: math.log((self._n - self._df.get(t, 0) + 0.5) / (self._df.get(t, 0) + 0.5) + 1)
            for t in toks
        }
        # 短名加成：query 词是否是某符号短名/文件名的子串（如 query "login" 命中 auth.login）
        name_hits: dict[str, float] = {}
        for i, d in enumerate(self.docs):
            base = (d["name"] + " " + d["file"]).lower()
            bonus = 0.0
            for t in toks:
                if t in base:
                    bonus += 2.0
            if bonus:
                name_hits[self.doc_id[i]] = bonus
        k1, b = 1.5, 0.75
        scores: dict[str, float] = {}
        for i, d in enumerate(self.docs):
            doc_id = self.doc_id[i]
            dl = self.doc_len[i]
            tf = self._tfs[i]
            s = 0.0
            for t in toks:
                f = tf.get(t, 0)
                if f:
                    s += idf[t] * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (self.avgdl or 1)))
            s += name_hits.get(doc_id, 0.0)
            if s > 0:
                scores[doc_id] = s
        ranked = sorted(scores.items(), key=lambda x: -x[1])[:k]
        return ranked


def build_index(conn, root: Path) -> dict[str, Any]:
    """从库中枚举所有 Function/Class，解析文件取代码片段，构建 BM25 索引。"""
    rows = dbmod._rows(conn, "MATCH (n:Function) RETURN n.file AS f UNION ALL MATCH (n:Class) RETURN n.file AS f", {})
    files = sorted({r["f"] for r in rows if r.get("f")})
    docs: list[Doc] = []
    for file in files:
        path = root / file
        if not path.exists():
            continue
        try:
            pf = parse_python(path.read_bytes(), file)
        except Exception:
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        mod = module_name(file)
        for s in pf.symbols:
            doc_id = s.fqn(mod)
            text, _ = _snippet(lines, s.start_line, s.end_line)
            docs.append({"id": doc_id, "name": s.name, "file": file, "text": text})
    return {
        "root": root,
        "index": BM25Index(docs),
        "docs": docs,
        "doc_by_id": {d["id"]: d for d in docs},
        "semantic": None,  # 向量后端槽位（M4 step2 填充）
    }


def semantic_search(index: dict[str, Any], query: str, k: int = 10) -> list[tuple[str, float]]:
    """语义向量检索。未接向量后端时返回空（hybrid 退化为纯 BM25）。"""
    backend = index.get("semantic")
    if backend is None:
        return []
    return backend(query, k)


def _rrf(ranked_lists: Sequence[Sequence[tuple[str, float]]], const: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion：按排名倒数和合并，再除常量取正。"""
    scores: dict[str, float] = {}
    for rl in ranked_lists:
        for rank, (doc_id, _score) in enumerate(rl, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (const + rank)
    return sorted(scores.items(), key=lambda x: -x[1])


def hybrid_search(index: dict[str, Any], query: str, k: int = 10) -> list[dict[str, Any]]:
    """BM25 + 语义向量 → RRF 融合。返回 [{id,name,file,score,sources}]。"""
    bm25 = index["index"].search(query, k * 3)
    sem = semantic_search(index, query, k * 3)
    fused = _rrf([bm25, sem])[:k]
    out: list[dict[str, Any]] = []
    for doc_id, score in fused:
        d = index["doc_by_id"].get(doc_id, {})
        out.append({
            "id": doc_id,
            "name": d.get("name", ""),
            "file": d.get("file", ""),
            "score": round(score, 4),
            "sources": _sources_of(doc_id, bm25, sem),
        })
    return out


def _sources_of(doc_id: str, bm25: list[tuple[str, float]], sem: list[tuple[str, float]]) -> list[str]:
    src: list[str] = []
    if any(i == doc_id for i, _ in bm25):
        src.append("bm25")
    if any(i == doc_id for i, _ in sem):
        src.append("semantic")
    return src or ["rrf"]
