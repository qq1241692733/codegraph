"""建图：把解析结果解析成节点 + 边（CALLS / CONTAINS / HAS_METHOD / IMPORTS）。

调用解析遵循"精度优先、宁可漏连不瞎连"（借鉴 GitNexus）：
未解析成功的调用丢弃并计入统计，而不是靠猜测硬连。
V1 边界：实例方法调用（svc.create_user）、动态调用、闭包等暂不覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .parser import CLASS, METHOD, CallSite, Import, ParsedFile, Symbol, module_of

# 关系类型
CALLS = "CALLS"
CONTAINS = "CONTAINS"
HAS_METHOD = "HAS_METHOD"
IMPORTS = "IMPORTS"


@dataclass
class GraphNode:
    id: str
    label: str  # File | Function | Class
    props: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    src: str
    dst: str
    type: str
    confidence: float = 1.0


@dataclass
class CodeGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    file_to_module: dict[str, str] = field(default_factory=dict)
    # 增量时 extra 符号不产生节点，但可能成为边的一端；记录其 label 供边路由
    extra_labels: dict[str, str] = field(default_factory=dict)


def module_name(rel_path: str) -> str:
    """相对路径 -> 模块名（多语言分发，见 parser.module_of）。"""
    return module_of(rel_path)


def build_graph(
    parsed_files: list[ParsedFile],
    extra_symbols: list[Symbol] | None = None,
    extra_files: list[str] | None = None,
) -> CodeGraph:
    """建图。extra_symbols/extra_files 是"未变文件"的符号与路径，只注入解析索引与
    file_of_module（供跨文件调用解析与 IMPORTS 边生成），不产生节点/边——
    增量写回时未变文件的行已在库中，不能重复重建。
    extra_files 覆盖**无符号**的未变文件（纯 import 中转模块），否则指向它们的 IMPORTS 边会丢。
    """
    extra_symbols = extra_symbols or []
    extra_files = extra_files or []
    g = CodeGraph()
    # 索引：module -> {fqn: Symbol} / {name: [Symbol]}；module -> imports
    by_fqn: dict[str, dict[str, Symbol]] = {}
    by_name: dict[str, dict[str, list[Symbol]]] = {}
    imports_of: dict[str, list[Import]] = {}
    modules_of_file = {pf.file: module_name(pf.file) for pf in parsed_files}
    g.file_to_module = modules_of_file
    file_of_module = {m: f for f, m in modules_of_file.items()}

    for pf in parsed_files:
        mod = modules_of_file[pf.file]
        by_fqn.setdefault(mod, {})
        by_name.setdefault(mod, {})
        for s in pf.symbols:
            by_fqn[mod][s.fqn(mod)] = s
            by_name[mod].setdefault(s.name, []).append(s)
        imports_of[mod] = pf.imports
        g.nodes.append(GraphNode(mod, "File", {"name": pf.file, "path": pf.file}))

    # 未变文件符号注入索引（setdefault 不覆盖已解析符号），并记录 label 供边路由
    for s in extra_symbols:
        mod = module_name(s.file)
        by_fqn.setdefault(mod, {})
        by_name.setdefault(mod, {})
        by_fqn[mod].setdefault(s.fqn(mod), s)
        by_name[mod].setdefault(s.name, []).append(s)
        g.extra_labels[s.fqn(mod)] = "Class" if s.kind == CLASS else "Function"
        # 边也可能指向未变文件自身（IMPORTS/CONTAINS 的 dst=File 节点）
        g.extra_labels.setdefault(mod, "File")
        # 未变文件的 module 也纳入 file_of_module，否则 reparse 文件指向它的 IMPORTS 边会丢
        file_of_module.setdefault(mod, s.file)

    # 无符号的未变文件（纯 import 中转）：其 module 也要进 file_of_module，保证 IMPORTS 边生成
    for f in extra_files:
        mod = module_name(f)
        file_of_module.setdefault(mod, f)
        g.extra_labels.setdefault(mod, "File")

    # 符号节点 + 归属边 + 方法边
    for pf in parsed_files:
        mod = modules_of_file[pf.file]
        for s in pf.symbols:
            fqn = s.fqn(mod)
            label = "Class" if s.kind == CLASS else "Function"
            g.nodes.append(GraphNode(fqn, label, {"name": s.name, "file": pf.file}))
            g.edges.append(GraphEdge(mod, fqn, CONTAINS))
            if s.kind == METHOD and s.parent:
                g.edges.append(GraphEdge(f"{mod}.{s.parent}", fqn, HAS_METHOD))

    # 文件级 IMPORTS 边（增量 importer 反查的基础）
    for pf in parsed_files:
        mod = modules_of_file[pf.file]
        for imp in imports_of[mod]:
            if imp.module in file_of_module:
                g.edges.append(GraphEdge(mod, imp.module, IMPORTS, confidence=0.9))

    # 调用解析 -> CALLS
    unresolved = 0
    for pf in parsed_files:
        mod = modules_of_file[pf.file]
        for call in pf.calls:
            target = _resolve(call, mod, by_fqn, by_name, imports_of)
            if target is None:
                unresolved += 1
                continue
            caller_fqn = f"{mod}.{call.caller}"
            g.edges.append(GraphEdge(caller_fqn, target, CALLS, confidence=1.0))
    g.props_unresolved = unresolved  # type: ignore[attr-defined]
    return g


def _resolve(
    call: CallSite,
    caller_module: str,
    by_fqn: dict[str, dict[str, Symbol]],
    by_name: dict[str, dict[str, list[Symbol]]],
    imports_of: dict[str, list[Import]],
) -> str | None:
    chain = call.callee_chain
    head = chain.split(".")[0]
    tail = ".".join(chain.split(".")[1:]) if "." in chain else ""

    # 1) 本模块直接匹配（短名唯一，或限定名命中）
    local = by_name.get(caller_module, {}).get(head, [])
    if len(local) == 1:
        return local[0].fqn(caller_module)
    for c in local:
        if c.qualified_name == chain:
            return c.fqn(caller_module)

    # 2) import 匹配：from X import Y / import X.Y
    for imp in imports_of.get(caller_module, []):
        local_name = imp.alias or imp.name
        if head != local_name:
            continue
        target_mod = imp.module
        name = tail or call.callee_name
        cands = by_name.get(target_mod, {}).get(name, [])
        if len(cands) == 1:
            return cands[0].fqn(target_mod)
        for c in cands:
            if c.name == name:
                return c.fqn(target_mod)

    # 3) 全库唯一短名兜底（有歧义则丢弃）
    globals_: list[str] = []
    for m, names in by_name.items():
        globals_.extend(c.fqn(m) for c in names.get(call.callee_name, []))
    return globals_[0] if len(globals_) == 1 else None
