"""Tree-sitter 解析器：从多语言源码提取符号、import 与调用点。

借鉴 GitNexus 的思路：不做正则硬解析，用 Tree-sitter AST + 统一中间结构
（Symbol / Import / CallSite / ParsedFile），让下游建图/查询与具体语法解耦。
语言分发借鉴 GitNexus 的 language-provider 注册表模式：按文件扩展名分发到
各语言的解析器，全部产出同一套 ParsedFile。目前支持 Python / JS / TS / Java。
"""
from __future__ import annotations

import os
import posixpath
import re
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tsp
import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tst
import tree_sitter_java as tsjava

_LANG_PY = Language(tsp.language())
_PARSER_PY = Parser(_LANG_PY)
_PARSER_JS = Parser(Language(tsjs.language()))
_PARSER_TS = Parser(Language(tst.language_typescript()))
_PARSER_TSX = Parser(Language(tst.language_tsx()))
_PARSER_JAVA = Parser(Language(tsjava.language()))

# 允许的符号类型
FUNCTION = "function"
METHOD = "method"
CLASS = "class"

# 扩展名 -> 语言
_EXT_LANG = {
    ".py": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "ts", ".tsx": "tsx",
    ".java": "java",
}

# 去掉语言文件扩展名的正则（用于模块命名）
_STRIP_EXT = re.compile(r"\.(py|js|mjs|cjs|jsx|ts|tsx|java)$")


@dataclass
class Symbol:
    name: str
    kind: str                 # function | method | class
    qualified_name: str       # 文件内作用域限定名，如 Service.create_user
    file: str
    start_line: int
    end_line: int
    parent: str | None = None  # 所属类（仅 method）

    def fqn(self, module: str) -> str:
        """模块级全限定名，如 auth.Service.create_user。"""
        return f"{module}.{self.qualified_name}" if module else self.qualified_name


@dataclass
class Import:
    module: str               # from auth import login -> auth；import os.path -> os.path
    name: str                 # 导入的短名（login / *）
    alias: str | None = None  # as lgn -> lgn
    is_from: bool = True      # True: from X import Y；False: import X.Y


@dataclass
class CallSite:
    caller: str               # 调用者符号的 qualified_name
    callee_name: str          # 归一化短名（链最后一段）
    callee_chain: str         # 完整调用链，如 service.helper / User(...)
    line: int


@dataclass
class ParsedFile:
    file: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)


def _by_field(n: Node, name: str) -> Node | None:
    return n.child_by_field_name(name)


def _named_children(n: Node) -> list[Node]:
    return [c for c in n.children if c.is_named]


def language_of(file) -> str | None:
    """按扩展名判定语言。"""
    file = os.fspath(file)
    return _EXT_LANG.get(file[file.rfind("."):] if "." in file else "", None)


def module_of(file) -> str:
    """相对路径 -> 模块名（多语言分发）。与 graph.module_name 同逻辑，内聚于此避免循环依赖。"""
    file = os.fspath(file)
    lang = language_of(file)
    p = file.replace("\\", "/")
    if lang == "python":
        if p.endswith(".py"):
            p = p[:-3]
        parts = [x for x in p.split("/") if x]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)
    # JS/TS/Java：目录点分路径（无包级 __init__ 概念；Java 的 package 由源码给出，V1 用路径近似）
    p = _STRIP_EXT.sub("", p)
    return ".".join(x for x in p.split("/") if x)


def parse(source: bytes, file: str) -> ParsedFile:
    """按文件语言分发解析，统一产出 ParsedFile。"""
    lang = language_of(file)
    if lang == "python":
        return parse_python(source, file)
    if lang in ("js", "ts", "tsx"):
        return _parse_js_ts(source, file, is_ts=(lang in ("ts", "tsx")))
    if lang == "java":
        return _parse_java(source, file)
    # 未知语言：退化为空 ParsedFile（不崩）
    return ParsedFile(file=file)


# ============================================================ Python
def parse_python(source: bytes, file: str) -> ParsedFile:
    tree = _PARSER_PY.parse(source)
    pf = ParsedFile(file=file)
    _walk_module_py(tree.root_node, pf)
    return pf


def _walk_module_py(node: Node, pf: ParsedFile) -> None:
    """只取模块级直接子节点中的符号与 import（条件内定义等暂不覆盖，V1 边界）。"""
    for child in node.children:
        if child.type == "function_definition":
            _walk_func_py(child, pf, parent_qual=None, in_class=False)
        elif child.type == "class_definition":
            _walk_class_py(child, pf, parent_qual=None)
        elif child.type == "import_statement":
            _collect_imports_py(child, pf, is_from=False)
        elif child.type == "import_from_statement":
            _collect_imports_py(child, pf, is_from=True)


def _walk_class_py(node: Node, pf: ParsedFile, parent_qual: str | None) -> None:
    name_node = _by_field(node, "name")
    name = name_node.text.decode() if name_node else ""
    qname = f"{parent_qual}.{name}" if parent_qual else name
    pf.symbols.append(
        Symbol(name, CLASS, qname, pf.file, node.start_point[0] + 1, node.end_point[0] + 1,
               parent=parent_qual)
    )
    body = _by_field(node, "body")
    if body is None:
        return
    for c in _named_children(body):
        if c.type == "function_definition":
            _walk_func_py(c, pf, parent_qual=qname, in_class=True)
        elif c.type == "class_definition":  # 嵌套类
            _walk_class_py(c, pf, parent_qual=qname)


def _walk_func_py(node: Node, pf: ParsedFile, parent_qual: str | None, in_class: bool) -> None:
    name_node = _by_field(node, "name")
    name = name_node.text.decode() if name_node else ""
    qname = f"{parent_qual}.{name}" if parent_qual else name
    kind = METHOD if in_class else FUNCTION
    pf.symbols.append(
        Symbol(name, kind, qname, pf.file, node.start_point[0] + 1, node.end_point[0] + 1,
               parent=parent_qual)
    )
    body = _by_field(node, "body")
    if body is not None:
        _collect_calls_py(body, pf, qname)


def _py_module_of(file: str) -> str:
    return module_of(file)


def _relative_module(file: str, rel_text: str) -> str:
    """把 Python 相对导入（如 '.'、'.order'、'..util'）解析成绝对模块名。"""
    dots = len(rel_text) - len(rel_text.lstrip("."))
    suffix = rel_text.lstrip(".")
    m = _py_module_of(file)
    if file.replace("\\", "/").endswith("__init__.py"):
        pkg = m
    else:
        parts = m.split(".") if m else []
        if parts:
            parts.pop()
        pkg = ".".join(parts)
    base_parts = pkg.split(".") if pkg else []
    for _ in range(dots - 1):
        if base_parts:
            base_parts.pop()
    base = ".".join(base_parts)
    if suffix:
        return f"{base}.{suffix}" if base else suffix
    return base


def _collect_imports_py(node: Node, pf: ParsedFile, is_from: bool) -> None:
    children = _named_children(node)
    if not children:
        return
    if is_from:
        first = children[0]
        name_nodes = children[1:]
        if first.type == "relative_import":
            rel = first.text.decode()
            base = _relative_module(pf.file, rel)
            per_name = not rel.lstrip(".")
        else:
            base = first.text.decode()
            per_name = False
        for c in name_nodes:
            if c.type == "wildcard_import":
                pf.imports.append(Import(module=base, name="*", is_from=True))
            elif c.type == "aliased_import":
                ns = _named_children(c)
                orig = ns[0].text.decode().split(".")[-1] if ns else ""
                alias = ns[-1].text.decode() if len(ns) > 1 else None
                module = f"{base}.{orig}" if per_name else base
                pf.imports.append(Import(module=module, name=orig, alias=alias, is_from=True))
            elif c.type == "dotted_name":
                nm = c.text.decode().split(".")[-1]
                module = f"{base}.{nm}" if per_name else base
                pf.imports.append(Import(module=module, name=nm, is_from=True))
    else:
        for c in children:
            if c.type == "aliased_import":
                ns = _named_children(c)
                orig = ns[0].text.decode() if ns else ""
                alias = ns[-1].text.decode() if len(ns) > 1 else None
                pf.imports.append(Import(module=orig, name=orig, alias=alias, is_from=False))
            elif c.type == "dotted_name":
                pf.imports.append(Import(module=c.text.decode(), name=c.text.decode(), is_from=False))


def _collect_calls_py(node: Node, pf: ParsedFile, caller: str) -> None:
    if node.type == "call":
        fn = _by_field(node, "function")
        if fn is not None:
            chain = fn.text.decode()
            callee = chain.split(".")[-1]
            pf.calls.append(
                CallSite(caller=caller, callee_name=callee, callee_chain=chain,
                         line=node.start_point[0] + 1)
            )
        for c in node.children:
            if c is not fn:
                _collect_calls_py(c, pf, caller)
        return
    for c in node.children:
        _collect_calls_py(c, pf, caller)


# ============================================================ JS / TS
def _parse_js_ts(source: bytes, file: str, is_ts: bool) -> ParsedFile:
    parser = _PARSER_TS if is_ts and file.endswith(".ts") else (
        _PARSER_TSX if file.endswith(".tsx") else _PARSER_JS)
    tree = parser.parse(source)
    pf = ParsedFile(file=file)
    _walk_program_js(tree.root_node, pf, is_ts=is_ts)
    return pf


def _walk_program_js(node: Node, pf: ParsedFile, is_ts: bool) -> None:
    for child in node.children:
        if child.type == "function_declaration":
            _walk_func_js(child, pf, parent_qual=None, in_class=False)
        elif child.type == "class_declaration":
            _walk_class_js(child, pf, parent_qual=None)
        elif child.type == "interface_declaration":
            _add_symbol(child, pf, CLASS, parent_qual=None)
        elif child.type == "import_statement":
            _collect_imports_js(child, pf)
        elif child.type in ("export_statement",):
            _walk_export_js(child, pf, is_ts)
        elif child.type == "lexical_declaration":
            _walk_variable_js(child, pf, parent_qual=None, in_class=False)


def _walk_export_js(node: Node, pf: ParsedFile, is_ts: bool) -> None:
    """export function/class/interface / export const f = ... —— 直接提取符号（不能当 program 递归）。"""
    for c in node.named_children:
        if c.type == "function_declaration":
            _walk_func_js(c, pf, parent_qual=None, in_class=False)
        elif c.type == "class_declaration":
            _walk_class_js(c, pf, parent_qual=None)
        elif c.type == "interface_declaration":
            _add_symbol(c, pf, CLASS, parent_qual=None)
        elif c.type == "lexical_declaration":
            _walk_variable_js(c, pf, parent_qual=None, in_class=False)
        # export {..} / export default expr 不产符号


def _add_symbol(node: Node, pf: ParsedFile, kind: str, parent_qual: str | None) -> None:
    n = _by_field(node, "name")
    name = n.text.decode() if n else ""
    if not name:
        return
    qname = f"{parent_qual}.{name}" if parent_qual else name
    pf.symbols.append(
        Symbol(name, kind, qname, pf.file, node.start_point[0] + 1, node.end_point[0] + 1,
               parent=parent_qual)
    )
    return name


def _walk_class_js(node: Node, pf: ParsedFile, parent_qual: str | None) -> None:
    name = _add_symbol(node, pf, CLASS, parent_qual)
    if not name:
        return
    qname = f"{parent_qual}.{name}" if parent_qual else name
    body = _by_field(node, "body")
    if body is None:
        return
    for c in _named_children(body):
        if c.type == "method_definition":
            mname = _add_symbol(c, pf, METHOD, qname)
            stmt = _by_field(c, "body")
            if mname and stmt is not None:
                _collect_calls_js(stmt, pf, f"{qname}.{mname}")


def _walk_func_js(node: Node, pf: ParsedFile, parent_qual: str | None, in_class: bool) -> None:
    name = _add_symbol(node, pf, METHOD if in_class else FUNCTION, parent_qual)
    if not name:
        return
    qname = f"{parent_qual}.{name}" if parent_qual else name
    body = _by_field(node, "body")
    if body is not None:
        _collect_calls_js(body, pf, qname)


def _walk_variable_js(node: Node, pf: ParsedFile, parent_qual: str | None, in_class: bool) -> None:
    """const f = () => {...} / const f = function(){...} —— 变量函数也记为符号。"""
    for d in _named_children(node):
        if d.type != "variable_declarator":
            continue
        name_node = _by_field(d, "name")
        value = _by_field(d, "value")
        if name_node is None or value is None:
            continue
        if value.type in ("arrow_function", "function_expression"):
            name = name_node.text.decode()
            qname = f"{parent_qual}.{name}" if parent_qual else name
            pf.symbols.append(
                Symbol(name, METHOD if in_class else FUNCTION, qname, pf.file,
                       node.start_point[0] + 1, node.end_point[0] + 1, parent=parent_qual)
            )
            body = _by_field(value, "body")
            if body is not None:
                _collect_calls_js(body, pf, qname)


def _collect_imports_js(node: Node, pf: ParsedFile) -> None:
    src = None
    clause = None
    for c in node.children:
        if c.type == "string":
            sf = c.child_by_field_name("value") or next((x for x in _named_children(c)), None)
            src = sf.text.decode() if sf is not None else c.text.decode().strip("'\"")
        elif c.type == "import_clause":
            clause = c
    if src is None:
        return
    module = _resolve_js_import(pf.file, src)
    if clause is None:  # `import './style.css'` 副作用导入
        pf.imports.append(Import(module=module, name="*", is_from=True))
        return
    # 默认导入：`import def from 'pkg'`
    default_node = _by_field(clause, "name")
    if default_node is not None:
        pf.imports.append(Import(module=module, name=default_node.text.decode(), is_from=True))
    for c in _named_children(clause):
        if c.type == "named_imports":
            for spec in _named_children(c):
                if spec.type == "import_specifier":
                    name_n = _by_field(spec, "name")
                    alias_n = _by_field(spec, "alias")
                    nm = name_n.text.decode() if name_n else ""
                    if nm:
                        pf.imports.append(Import(module=module, name=nm,
                                                 alias=alias_n.text.decode() if alias_n else None,
                                                 is_from=True))


def _resolve_js_import(file: str, spec: str) -> str:
    """把 JS 相对导入（'./util'、'../x'）解析成绝对模块名；bare specifier（'react'）保持。"""
    if not (spec.startswith("./") or spec.startswith("../")):
        return spec
    p = file.replace("\\", "/")
    cur_dir = p.rsplit("/", 1)[0] if "/" in p else ""
    combined = posixpath.normpath(posixpath.join(cur_dir, spec))
    combined = _STRIP_EXT.sub("", combined)
    return ".".join(x for x in combined.split("/") if x)


def _collect_calls_js(node: Node, pf: ParsedFile, caller: str) -> None:
    if node.type == "call_expression":
        fn = _by_field(node, "function")
        if fn is not None:
            chain = fn.text.decode()
            callee = chain.split(".")[-1]
            pf.calls.append(
                CallSite(caller=caller, callee_name=callee, callee_chain=chain,
                         line=node.start_point[0] + 1)
            )
        for c in node.children:
            if c is not fn:
                _collect_calls_js(c, pf, caller)
        return
    for c in node.children:
        _collect_calls_js(c, pf, caller)


# ============================================================ Java
def _parse_java(source: bytes, file: str) -> ParsedFile:
    tree = _PARSER_JAVA.parse(source)
    pf = ParsedFile(file=file)
    _walk_program_java(tree.root_node, pf)
    return pf


def _walk_program_java(node: Node, pf: ParsedFile) -> None:
    for child in node.children:
        if child.type == "class_declaration":
            _walk_class_java(child, pf, parent_qual=None)
        elif child.type == "interface_declaration":
            _add_symbol(child, pf, CLASS, parent_qual=None)
        elif child.type == "import_declaration":
            _collect_imports_java(child, pf)


def _walk_class_java(node: Node, pf: ParsedFile, parent_qual: str | None) -> None:
    name = _add_symbol(node, pf, CLASS, parent_qual)
    if not name:
        return
    qname = f"{parent_qual}.{name}" if parent_qual else name
    body = _by_field(node, "body")
    if body is None:
        return
    for c in _named_children(body):
        if c.type == "method_declaration":
            mname = _add_symbol(c, pf, METHOD, qname)
            if mname:
                _collect_calls_java(c, pf, f"{qname}.{mname}")
        elif c.type == "constructor_declaration":
            mname = _add_symbol(c, pf, METHOD, qname)
            if mname:
                _collect_calls_java(c, pf, f"{qname}.{mname}")


def _collect_imports_java(node: Node, pf: ParsedFile) -> None:
    scoped = next((c for c in node.named_children if c.type == "scoped_identifier"), None)
    if scoped is None:
        return
    full = scoped.text.decode()
    parts = full.split(".")
    name = parts[-1]
    pkg = ".".join(parts[:-1])  # import 的是类，IMPORTS 边指向其所在包（匹配文件 module）
    pf.imports.append(Import(module=pkg, name=name, is_from=False))


def _collect_calls_java(node: Node, pf: ParsedFile, caller: str) -> None:
    if node.type == "method_invocation":
        name_n = _by_field(node, "name")
        obj = _by_field(node, "object")
        if name_n is not None:
            nm = name_n.text.decode()
            chain = f"{obj.text.decode()}.{nm}" if obj is not None else nm
            pf.calls.append(CallSite(caller=caller, callee_name=nm, callee_chain=chain,
                                     line=node.start_point[0] + 1))
        for c in node.children:
            _collect_calls_java(c, pf, caller)
        return
    for c in node.children:
        _collect_calls_java(c, pf, caller)


# ============================================================ 文件发现 + 并行分块解析
# 分块策略借鉴 GitNexus worker-pool：按"文件数 + 字节上限"双约束分块，
# 进程池并行（每个子进程加载自己的 tree-sitter，规避 GIL 解析串行）。
SUB_BATCH_SIZE = 1500          # 单个 worker 分块的文件数上限
SUB_BATCH_MAX_BYTES = 8 * 1024 * 1024  # 单分块字节上限，防 worker 内存爆炸


def discover_sources(root) -> list:
    """扫描 root 下所有受支持语言的文件（.py/.js/.ts/.tsx/.java），忽略隐藏路径。"""
    root = os.fspath(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if fn[fn.rfind("."):] in _EXT_LANG:
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _parse_bytes(item) -> ParsedFile:
    """进程池 worker 入口：读文件字节 + 按语言分发解析（顶层可 pickle）。"""
    path, rel = item
    with open(path, "rb") as f:
        return parse(f.read(), rel)


def _chunk_items(items, max_files: int = SUB_BATCH_SIZE,
                 max_bytes: int = SUB_BATCH_MAX_BYTES) -> list[list]:
    chunks: list[list] = []
    cur: list = []
    cur_bytes = 0
    for it in items:
        size = os.path.getsize(it[0])
        if cur and (len(cur) >= max_files or cur_bytes + size > max_bytes):
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(it)
        cur_bytes += size
    if cur:
        chunks.append(cur)
    return chunks


def parse_many(files, root, workers: int | None = None) -> list[ParsedFile]:
    """并行分块解析一批文件，返回按输入顺序的 ParsedFile 列表。

    文件数过少或单 worker 时退化为顺序解析（进程池启动开销不划算）；
    大仓库按 sub-batch 分块交给进程池并行。
    """
    items = [(os.fspath(f), os.path.relpath(os.fspath(f), os.fspath(root))) for f in files]
    if not items:
        return []
    workers = workers or min((os.cpu_count() or 1), 8)
    if len(items) == 1 or workers == 1:
        return [parse(p.read_bytes(), rel) for p, rel in items]
    chunks = _chunk_items(items)
    parsed: list[ParsedFile] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for chunk in chunks:
            parsed.extend(ex.map(_parse_bytes, chunk))
    return parsed
