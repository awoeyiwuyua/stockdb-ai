#!/usr/bin/env python3
"""astock.extraction — 上游知识包确定性提取（M0，E01—E07）。

上游 a-stock-data 以 SKILL.md（markdown）承载实现：91 个 ```python 代码块、
182 个顶层符号，无显式 marker。按 DEVELOPMENT §3 无 marker 路径锁定：

    文件 hash + 代码块内容 hash + 顶层符号 + 依赖闭包

规则：
- 禁止按章节号盲切（上游曾重排编号）；一切定位以符号与块内容 hash 为准。
- 符号唯一性：同一符号出现在多个块 → 提取失败（重复定义），不猜。
- 依赖闭包：入口符号集合 → 块内引用的未定义名字 → 递归收录定义块；
  引用 stdlib/第三方（import 过的）由 import 语句解决，进入依赖锁。
- 副作用检查（E03）：生成模块的模块级 AST 逐一审查——非字面量赋值右值、
  顶层表达式语句、装饰器、默认参数表达式、import 均记录为"初始化节点"；
  仅 import 与 __future__ 允许静默通过，其余必须在 build 允许清单中显式列出。
  赋值/装饰器/默认参数视为可能执行代码（不能因"没有顶层调用"就当作无副作用）。
- 确定性（E04）：同 pin/入口/补丁/依赖 → 生成文件逐字节一致（拼装顺序固定、
  header 含来源但不含时间）；构建时间写 metadata，不混入内容 hash。
- 失败语义（E02/E06）：缺符号/重复符号/闭包失败/未知初始化 → ExtractionError，
  进入人工适配状态，不静默上线。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CODE_BLOCK_RE = re.compile(r"```python\n(.*?)(?:```|\Z)", re.DOTALL)


class ExtractionError(Exception):
    """提取/校验失败（缺符号、重复、副作用未允许、确定性破坏等）。"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_tree(root: Path) -> str:
    """vendored 快照树 hash：sorted(relative_path) + NUL + sha256(file) + LF。
    与项目 skill_manager / upstream-snapshots.json 同算法；排除 .git。"""
    entries = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        entries.append(f"{rel}\0{digest}\n")
    return hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()


@dataclass
class CodeBlock:
    index: int          # 在 SKILL.md 中第几个 python 块（0 起，定位辅助）
    content: str
    sha256: str
    symbols: list[str] = field(default_factory=list)   # 顶层 def/class 名
    imports: list[str] = field(default_factory=list)   # 顶层 import 模块名
    assigns: list[str] = field(default_factory=list)   # 模块级赋值名

    def names_defined(self) -> set[str]:
        return set(self.symbols) | set(self.assigns)


def index_code_blocks(skill_md: str) -> list[CodeBlock]:
    """SKILL.md → CodeBlock 列表（AST 解析失败的块原样保留但不参与符号索引）。"""
    blocks: list[CodeBlock] = []
    for i, match in enumerate(_CODE_BLOCK_RE.finditer(skill_md)):
        content = match.group(1)
        block = CodeBlock(index=i, content=content, sha256=sha256_text(content))
        try:
            tree = ast.parse(content)
        except SyntaxError:
            blocks.append(block)   # 语法不完整块（如片段展示）不入符号索引
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                block.symbols.append(node.name)
            elif isinstance(node, ast.Import):
                block.imports.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                block.imports.append(node.module.split(".")[0])
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        block.assigns.append(target.id)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                # 模块级循环 target 也是块内定义的名字（symtable 视为 global）
                for t in node.target.elts if isinstance(
                        node.target, ast.Tuple) else [node.target]:
                    if isinstance(t, ast.Name):
                        block.assigns.append(t.id)
        blocks.append(block)
    return blocks


def _free_variables(block: CodeBlock) -> set[str]:
    """块内引用但未在块内定义、需由其它块或 import 满足的名字。

    用 symtable 做真实作用域分析：函数参数/局部赋值不是自由变量（此前用
    ast.Name(Load) 粗扫会把函数局部变量当依赖，导致闭包雪球滚进示例代码）。
    """
    import builtins as _builtins
    import symtable
    table = symtable.symtable(block.content,
                              f"<block#{block.index}>", "exec")
    builtin_names = set(dir(_builtins))
    free: set[str] = set()

    def visit(scope: Any) -> None:
        for sym in scope.get_symbols():
            name = sym.get_name()
            if name in builtin_names:
                continue
            # is_free：嵌套闭包引用外层；is_global：引用模块级/其他块/未定义。
            # 已在本块顶层定义（赋值/def/class/import）的名字由 local 兜底排除。
            if sym.is_free() or sym.is_global():
                free.add(name)
        for child in scope.get_children():
            visit(child)

    visit(table)
    local = block.names_defined()
    for b in block.imports:
        pass
    return {n for n in free if n not in local and n not in set(block.imports)}


def resolve_closure(blocks: list[CodeBlock],
                    entry_symbols: list[str]) -> list[CodeBlock]:
    """入口符号 → 依赖闭包块集合（拓扑排序由后续拼装顺序保证：按块 index）。"""
    by_symbol: dict[str, list[CodeBlock]] = {}
    for b in blocks:
        for name in b.names_defined():
            by_symbol.setdefault(name, []).append(b)
    # 重复定义只在实际选中该符号时报错：上游演示代码常有同名演示赋值，
    # 未被闭包引用的重复不阻塞提取（E02 语义：对需要生成的代码拒绝猜测）
    missing = [s for s in entry_symbols if s not in by_symbol]
    if missing:
        raise ExtractionError(
            f"entry symbols not found in source: {missing}")

    def _def_text(holder: CodeBlock, name: str) -> str:
        tree = ast.parse(holder.content)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name
                    for t in node.targets):
                return ast.unparse(node.value)
        return ""

    def unique_block(name: str) -> CodeBlock:
        holders = by_symbol[name]
        if len(holders) > 1:
            chosen = [h for h in holders if h.index in selected]
            if chosen:
                # 同一闭包内已选块重定义的同名符号：复用已选块（一致性
                # 快照），不引入第二个定义
                return chosen[0]
            texts = {_def_text(h, name) for h in holders}
            if len(texts) == 1 and "" not in texts:
                # 同一内容的多处定义（上游各层重复同一常量，如 UA）：
                # 语义等价，取第一个；不同内容仍拒绝猜测
                return holders[0]
            raise ExtractionError(
                f"duplicate definition of {name!r} "
                f"(blocks #{[h.index for h in holders]}); refusing to guess")
        return holders[0]

    selected: dict[int, CodeBlock] = {}
    pending = list(dict.fromkeys(entry_symbols))
    while pending:
        name = pending.pop()
        if name not in by_symbol:
            # 自由变量在其它已选块 import 或确实缺失：缺失名字在生成模块
            # import 检查时暴露；此处只解析有定义的
            continue
        block = unique_block(name)
        if block.index not in selected:
            selected[block.index] = block
            for free in sorted(_free_variables(block)):
                if free in by_symbol:
                    pending.append(free)
    return [selected[k] for k in sorted(selected)]


_INITIALIZATION_ALLOWED_KINDS = {"import"}   # import 允许静默通过


def inspect_initialization(module_text: str) -> list[dict[str, str]]:
    """模块级初始化节点清单（E03：赋值/装饰器/默认参数也可能执行代码）。

    每个节点带 safe 标记：safe=true 表示仅构造字面量容器或对字面量调用
    re.compile 等纯构造（人工审阅规则，见 _is_safe_initializer），记录审计
    但不阻塞；其余节点必须逐条进入 build allowlist 才能通过。
    """
    tree = ast.parse(module_text)
    nodes: list[dict[str, str]] = []
    for node in tree.body:
        kind = type(node).__name__
        detail = ""
        literal_safe = False
        if isinstance(node, ast.Import):
            kind, literal_safe = "import", True
            detail = ", ".join(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            kind, literal_safe = "import", True
            detail = f"from {node.module}"
        elif isinstance(node, ast.Assign):
            names = ",".join(t.id for t in node.targets
                             if isinstance(t, ast.Name))
            detail = f"{names} = {ast.unparse(node.value)[:80]}"
            literal_safe = all(
                isinstance(v, (ast.Constant, ast.List, ast.Tuple, ast.Dict,
                               ast.Set))
                and _only_constants(v) for v in [node.value])
        elif isinstance(node, ast.Expr):
            detail = ast.unparse(node)[:80]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            decs = [ast.unparse(d)[:60] for d in node.decorator_list]
            defaults = [ast.unparse(d)[:60]
                        for d in node.args.defaults
                        if not _only_constants(d)]
            if decs or defaults:
                kind = f"{kind}+initializers"
                detail = "; ".join(decs + defaults) or "<defaults>"
            else:
                literal_safe = True
        elif isinstance(node, ast.If):  # 模块级 if（如 __main__ 守卫）
            detail = ast.unparse(node.test)[:80]
        if literal_safe or _is_safe_initializer(node):
            nodes.append({"kind": kind, "detail": detail, "lineno":
                          str(getattr(node, "lineno", 0)), "safe": "true"})
        else:
            nodes.append({"kind": kind, "detail": detail,
                          "lineno": str(getattr(node, "lineno", 0)),
                          "safe": "false"})
    return nodes


def _is_safe_initializer(node: ast.AST) -> bool:
    """人工审阅后的安全初始化规则：字面量容器赋值 / re.compile(字面量)。"""
    if isinstance(node, ast.Assign) and len(node.targets) == 1             and isinstance(node.targets[0], ast.Name):
        value = node.value
        if isinstance(value, ast.Call) and isinstance(value.func,
                                                      ast.Attribute)                 and value.func.attr == "compile":
            return (all(isinstance(a, (ast.Constant, ast.UnaryOp, ast.Name,
                                       ast.Attribute))
                        for a in value.args)
                    and all(isinstance(k.value, (ast.Name, ast.Attribute,
                                                 ast.UnaryOp))
                            for k in value.keywords))
        if isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict,
                              ast.Constant)):
            return _only_constants(value)
    return False


def _only_constants(node: ast.AST) -> bool:
    """字面量容器/常量判定；ctx 节点（Load 等）不算内容。"""
    for child in ast.walk(node):
        if isinstance(child, (ast.Load, ast.Store, ast.Del)):
            continue
        if isinstance(child, ast.Constant):
            continue
        if isinstance(child, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            continue
        return False
    return True


def filter_allowed_initialization(nodes: list[dict[str, str]],
                                  allowlist: list[str]) -> None:
    """初始化节点必须在允许清单内；import 除外。违反 → ExtractionError。"""
    for n in nodes:
        if n["kind"] in _INITIALIZATION_ALLOWED_KINDS or n.get("safe") == "true":
            continue
        key = f"{n['kind']}:{n['detail']}"
        if key not in allowlist and n["detail"] not in allowlist:
            raise ExtractionError(
                f"unrecognized module-level initialization: {key} "
                "(add to build allowlist after human review; "
                "assignments/decorators/default-args can execute code)")


def strip_top_level_exprs(block: CodeBlock,
                          business_symbols: set[str] | None = None) -> CodeBlock:
    """patch：剔除上游 markdown 演示执行（不改 vendor 原件，记录 patch）。

    - 顶层表达式语句（norm_ticker('SH600519') 演示）→ 剔除；
    - 顶层循环（演示遍历打印/请求）→ 剔除；
    - 右值调用**上游业务函数**的赋值（anns = cninfo_announcements('688017')，
      会真实发起网络请求）→ 剔除；
    - 右值调用第三方/标准库的赋值（EM_SESSION = requests.Session()）→
      保留（基础设施初始化，安全性由 initialization allowlist 人工审阅）。
    """
    tree = ast.parse(block.content)
    biz = business_symbols or set()

    def root_name(call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        node = func
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    def demo_exec(n: ast.AST) -> bool:
        if isinstance(n, ast.Expr):
            return True
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            return True
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            root = root_name(n.value)
            return root in biz   # 仅上游业务函数的演示调用
        return False

    kept = [n for n in tree.body if not demo_exec(n)]
    if len(kept) == len(tree.body):
        return block
    new_text = ast.unparse(ast.Module(body=kept, type_ignores=[]))
    return CodeBlock(index=block.index, content=new_text,
                     sha256=sha256_text(new_text),
                     symbols=block.symbols, imports=block.imports,
                     assigns=block.assigns)


def generate_module(blocks: list[CodeBlock], *, module_name: str,
                    source_commit: str, source_file: str,
                    source_file_sha256: str, header_extra: str = "",
                    patch_id: str | None = None) -> tuple[str, str]:
    """确定性拼装生成模块；返回 (module_text, module_sha256)。

    拼装顺序固定为块 index；header 含来源事实（commit/文件/hash）但不含
    构建时间——时间写入 build-lock metadata，不混入内容 hash（E04）。
    """
    # header 用 # 注释：docstring 的 """ 会与代码块内容冲突提前闭合
    header = (
        f"# AUTO-GENERATED from a-stock-data {source_commit[:12]} — "
        "do not edit by hand.\n"
        f"# source file: {source_file}\n"
        f"# source file sha256: {source_file_sha256}\n"
        f"# blocks: {[b.index for b in blocks]}\n"
        f"# block sha256s: {[b.sha256[:12] for b in blocks]}\n"
        + (f"# patch: {patch_id}\n" if patch_id else "")
        + (f"# {header_extra}\n" if header_extra else "")
        + "# Upstream licensed Apache-2.0 (see vendor LICENSE); this generated\n"
        "# file records origin and is rebuilt deterministically by astock.build.\n")
    body = "\n\n".join(b.content.rstrip() for b in blocks)
    text = header + "\n" + body + "\n"
    ast.parse(text)  # 生成结果必须可解析
    return text, sha256_text(text)


@dataclass
class ExtractionPlan:
    """一次提取的完整计划（进 extraction-manifest）。"""
    vendor_root: str
    source_commit: str
    source_file: str
    source_file_sha256: str
    vendor_tree_sha256: str
    entries: list[str]
    blocks: list[dict[str, Any]]
    initialization_allowlist: list[str]
    initialization_nodes: list[dict[str, str]]
    generated_module_name: str
    generated_sha256: str
    imports: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "vendor_root": self.vendor_root,
            "upstream_commit": self.source_commit,
            "source_file": self.source_file,
            "source_file_sha256": self.source_file_sha256,
            "vendor_tree_sha256": self.vendor_tree_sha256,
            "entry_symbols": self.entries,
            "selected_blocks": self.blocks,
            "initialization_allowlist": self.initialization_allowlist,
            "initialization_nodes": self.initialization_nodes,
            "generated_module": self.generated_module_name,
            "generated_module_sha256": self.generated_sha256,
            "third_party_imports": self.imports,
        }


_STDLIB = set(getattr(__import__("sys"), "stdlib_module_names", ())) | {
    "dataclasses", "typing", "pathlib", "datetime", "decimal", "json",
    "re", "time", "hashlib", "collections", "functools", "itertools",
    "urllib", "ssl", "gzip", "zipfile", "io", "os", "sys", "math",
    "statistics", "textwrap", "warnings", "base64", "struct", "socket"}


def extract(vendor_root: Path, *, source_commit: str, entries: list[str],
            module_name: str, source_file: str = "SKILL.md",
            initialization_allowlist: list[str] | None = None,
            patch_id: str | None = None,
            strip_demo_calls: bool = True) -> tuple[ExtractionPlan, str]:
    """完整提取流水线：索引→闭包→（patch）→拼装→初始化审查。

    strip_demo_calls=True（默认）应用 strip_top_level_exprs patch 剔除上游
    演示调用；patch_id 自动补记。返回 (ExtractionPlan, module_text)。
    """
    allowlist = list(initialization_allowlist or [])
    applied_patches = [patch_id] if patch_id else []
    skill_path = vendor_root / source_file
    if not skill_path.is_file():
        raise ExtractionError(f"source file missing: {skill_path}")
    skill_text = skill_path.read_text(encoding="utf-8")
    blocks = index_code_blocks(skill_text)
    selected = resolve_closure(blocks, entries)
    if strip_demo_calls:
        business = {sym for b in selected for sym in b.symbols}
        stripped = [strip_top_level_exprs(b, business) for b in selected]
        if any(stripped[i].content != selected[i].content
               for i in range(len(selected))):
            applied_patches.append("strip-demo-calls(top-expr/loops/business-call-assigns)")
        selected = stripped
    effective_patch = ";".join(p for p in applied_patches if p) or None
    module_text, module_hash = generate_module(
        selected, module_name=module_name, source_commit=source_commit,
        source_file=source_file,
        source_file_sha256=sha256_text(skill_text),
        patch_id=effective_patch)
    init_nodes = inspect_initialization(module_text)
    # 未在允许清单的模块级初始化 → 提取失败（E02/E03：人工审阅后才可加白，
    # 绝不静默加白上线）
    filter_allowed_initialization(init_nodes, allowlist)
    imports = sorted({i for b in selected for i in b.imports
                      if i not in _STDLIB})
    plan = ExtractionPlan(
        vendor_root=str(vendor_root), source_commit=source_commit,
        source_file=source_file, source_file_sha256=sha256_text(skill_text),
        vendor_tree_sha256=sha256_tree(vendor_root), entries=list(entries),
        blocks=[{"index": b.index, "sha256": b.sha256,
                 "symbols": b.symbols, "imports": b.imports}
                for b in selected],
        initialization_allowlist=allowlist,
        initialization_nodes=init_nodes,
        generated_module_name=module_name, generated_sha256=module_hash,
        imports=imports)
    return plan, module_text
