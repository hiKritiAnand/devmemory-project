"""Code-structure knowledge graph: files, top-level symbols, imports, and
best-effort call edges, stored alongside devmemory's decision records in the
same SQLite store so the two can cross-reference the same file+symbol keys.

Each language is a `LanguageSpec`: data describing which tree-sitter node
types mean "import"/"definition"/"call" for that grammar, plus small
override functions only where a grammar's own field/node conventions don't
fit the common shape (see the per-language comments below -- each fact was
verified against real tree-sitter output before being encoded here, not
guessed). `_generic_walk` is the one walker every language shares.

Import and call/reference edges are best-effort everywhere, by design:
- Python imports resolve by dotted-path suffix matching against files found
  in the repo. JS/TS/C/C++ imports resolve by relative path (common
  extensions tried for JS/TS; for C/C++ this naturally handles both
  `#include "local.h"` and `<system.h>` -- the latter simply never resolves
  and falls back to `external`, with no need to special-case the syntax).
  Java/C#/Go/PHP imports are NOT resolved to local files in V1 -- each has
  its own package/classpath/namespace resolution scheme that doesn't reduce
  to path concatenation the way Python/JS do, so guessing would often be
  wrong; they're always recorded as `external` nodes instead, honestly.
- `export ... from "..."` re-exports (JS/TS) aren't treated as imports.
- Call edges are syntactic: a call only becomes a `calls` edge if the
  callee is a symbol defined in the same file. No type resolution, so
  overloads, dynamic dispatch, and calls on values of unresolved type are
  missed. For most languages, a call/reference outside any function body
  isn't attributed to a caller and is skipped -- except SQL (see its spec
  below), where most real files are top-level statements, not function
  bodies, so an un-enclosed reference is attributed to the file itself
  rather than silently dropped.
This is the same honesty `storage.query_decisions`'s substring symbol match
already applies elsewhere in devmemory.
"""

import hashlib
import os
import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tree_sitter_language_pack import get_parser

from . import git_utils, ignore_config, retrieval, storage

KNOWLEDGE_RELATIVE_DIR = Path(".devmemory") / "knowledge"
MERMAID_RELATIVE_PATH = KNOWLEDGE_RELATIVE_DIR / "graph.mmd"

VENDOR_DIR_NAMES = {".git", ".devmemory", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


@dataclass
class Definition:
    kind: str  # "function" | "class" | "method" | "interface" | "table"
    name: str
    line_start: int
    line_end: int
    signature: str | None
    docstring: str | None
    # SHA-256 of the definition's own source span (not the whole file) --
    # used for lighter-weight node versioning (Phase 6): detecting that a
    # named symbol's body actually changed, and matching a disappeared
    # symbol against a newly-appeared one as a rename candidate. Only set
    # for languages parsed via _generic_walk; custom-parsed languages
    # (HTML/CSS/SQL) leave it None, which simply disables versioning for
    # them rather than guessing.
    body_hash: str | None = None


@dataclass
class ParsedFile:
    imports: list[str] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    calls: list[tuple[str | None, str]] = field(default_factory=list)  # (caller_symbol_or_None, callee_name)
    references: list[tuple[str | None, str]] = field(default_factory=list)  # SQL only: (caller_or_None, table_name)
    inherits: list[tuple[str, str, str]] = field(default_factory=list)  # (class_name, base_name, "extends"|"implements")


def get_knowledge_dir(root) -> Path:
    return Path(root) / KNOWLEDGE_RELATIVE_DIR


def get_mermaid_path(root) -> Path:
    return Path(root) / MERMAID_RELATIVE_PATH


def _text(src: bytes, node) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


def _extract_docstring(body_node, src: bytes) -> str | None:
    if body_node is None or not body_node.children:
        return None
    first = body_node.children[0]
    if first.type == "expression_statement" and first.children:
        text = _text(src, first.children[0])
        if text and text[0] in "\"'":
            return text.strip("\"'").strip() or None
    return None


def _default_name(node, src: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    return _text(src, name_node) if name_node else None


def _default_params(node, src: bytes) -> str | None:
    params_node = node.child_by_field_name("parameters")
    return _text(src, params_node) if params_node else None


def _shorten_callee(text: str) -> str:
    # `a.b.c()` -> "c"; `Helper::round()` -> "round"; plain `foo()` -> "foo".
    return re.split(r"\.|::", text)[-1]


def _default_callee(node, src: bytes) -> str | None:
    fn = node.child_by_field_name("function")
    return _shorten_callee(_text(src, fn)) if fn is not None else None


def _name_field_callee(node, src: bytes) -> str | None:
    """Java's method_invocation and PHP's member/scoped call expressions put
    the callee directly in the `name` field, not `function`."""
    name = node.child_by_field_name("name")
    return _text(src, name) if name is not None else None


@dataclass
class LanguageSpec:
    extensions: list[str]
    def_node_types: dict[str, str]  # tree-sitter type -> our kind
    import_node_types: set[str] = field(default_factory=set)
    call_node_types: set[str] = field(default_factory=set)
    reference_node_types: set[str] = field(default_factory=set)
    extract_import: Callable[[object, bytes], list[str]] | None = None
    extract_def_name: Callable[[object, bytes], str | None] = _default_name
    extract_def_params: Callable[[object, bytes], str | None] = _default_params
    extract_callee: Callable[[object, bytes], str | None] = _default_callee
    extract_reference: Callable[[object, bytes], str | None] | None = None
    # Given the definition node itself (class/interface), not a separately
    # node-type-keyed child -- so the extractor always knows whether it's
    # looking at a class or an interface (needed e.g. for C#, where the
    # grammar doesn't distinguish a base class from an implemented interface
    # syntactically). Returns [(base_name, "extends"|"implements"), ...].
    extract_inherit: Callable[[object, bytes], list[tuple[str, str]]] | None = None
    import_resolution: str = "external"  # "dotted" | "relative" | "external"
    attribute_top_level_to_file: bool = False
    custom_parser: Callable[[object, bytes], ParsedFile] | None = None


def _generic_walk(root_node, src: bytes, spec: LanguageSpec) -> ParsedFile:
    parsed = ParsedFile()

    def record(collection, enclosing, target):
        if enclosing is not None:
            collection.append((enclosing, target))
        elif spec.attribute_top_level_to_file:
            collection.append((None, target))

    def walk(node, enclosing: str | None):
        if node.type in spec.import_node_types:
            parsed.imports.extend(spec.extract_import(node, src))
            return
        if node.type in spec.def_node_types:
            name = spec.extract_def_name(node, src)
            if name:
                body_node = node.child_by_field_name("body")
                # Hash the body only, not the whole definition span: the
                # span includes the name/signature itself, which would
                # defeat the entire point of body-hash rename detection (a
                # pure rename, logic unchanged, must still hash identically).
                # Falls back to the whole node for a definition kind with no
                # "body" field (e.g. a plain type alias).
                hash_source = body_node if body_node is not None else node
                parsed.definitions.append(Definition(
                    kind=spec.def_node_types[node.type],
                    name=name,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                    signature=spec.extract_def_params(node, src),
                    docstring=_extract_docstring(body_node, src),
                    body_hash=hashlib.sha256(src[hash_source.start_byte:hash_source.end_byte]).hexdigest(),
                ))
                if spec.extract_inherit is not None:
                    for base_name, rel_kind in spec.extract_inherit(node, src):
                        if base_name:
                            parsed.inherits.append((name, base_name, rel_kind))
                enclosing = name
        if node.type in spec.call_node_types:
            callee = spec.extract_callee(node, src)
            if callee:
                record(parsed.calls, enclosing, callee)
        if node.type in spec.reference_node_types:
            target = spec.extract_reference(node, src)
            if target:
                record(parsed.references, enclosing, target)
        for child in node.children:
            walk(child, enclosing)

    walk(root_node, enclosing=None)
    return parsed


# --- Python -----------------------------------------------------------

def _python_import_targets(node, src: bytes) -> list[str]:
    targets = []
    if node.type == "import_statement":
        for child in node.children:
            if child.type == "dotted_name":
                targets.append(_text(src, child))
            elif child.type == "aliased_import" and child.children:
                targets.append(_text(src, child.children[0]))
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        if module is not None:
            targets.append(_text(src, module))
    return [t for t in targets if t and t != "."]


def _python_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """Python has no separate `implements` concept (no interface keyword) --
    every base in `class Foo(Bar, Baz)` is "extends", including ABCs/mixins."""
    if node.type != "class_definition":
        return []
    bases = node.child_by_field_name("superclasses")
    if bases is None:
        return []
    results = []
    for child in bases.children:
        if child.type in ("identifier", "attribute"):
            results.append((_shorten_callee(_text(src, child)), "extends"))
    return results


# --- JavaScript / TypeScript / TSX -------------------------------------

def _js_import_target(node, src: bytes) -> list[str]:
    for child in node.children:
        if child.type == "string":
            text = _text(src, child).strip("\"'`")
            if text:
                return [text]
    return []


def _js_requires(node, src: bytes) -> list[str]:
    fn = node.child_by_field_name("function")
    if fn is None or _text(src, fn) != "require":
        return []
    args = node.child_by_field_name("arguments")
    if args is None:
        return []
    return [_text(src, arg).strip("\"'`") for arg in args.children if arg.type == "string"]


def _parse_js_like(root_node, src: bytes, spec: "LanguageSpec") -> ParsedFile:
    """JS/TS need one extra thing _generic_walk doesn't model: a plain call
    whose callee is literally `require` is itself an import, not a call."""
    parsed = _generic_walk(root_node, src, spec)
    # _generic_walk already recorded require(...) calls as bogus `calls`
    # entries (callee name "require") since call_expression is also our call
    # type; strip those and add the real import targets instead.
    parsed.calls = [(c, n) for c, n in parsed.calls if n != "require"]

    def find_requires(node):
        if node.type == "call_expression":
            parsed.imports.extend(_js_requires(node, src))
        for child in node.children:
            find_requires(child)

    find_requires(root_node)
    return parsed


def _js_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """Plain JS has no `implements` keyword -- `class_heritage` here is just
    `extends <expr>` directly (no extends_clause wrapper), unlike TS/TSX
    (see _ts_inherit) where the grammar splits extends_clause/implements_clause
    apart. Verified against real tree-sitter output: plain JS's class_heritage
    children are literally [`extends` keyword, the superclass expression]."""
    if node.type != "class_declaration":
        return []
    for child in node.children:
        if child.type == "class_heritage":
            heritage_children = list(child.children)
            for i, c in enumerate(heritage_children):
                if c.type == "extends" and i + 1 < len(heritage_children):
                    return [(_shorten_callee(_text(src, heritage_children[i + 1])), "extends")]
    return []


def _ts_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """TS/TSX class_heritage wraps extends_clause (field `value`) and/or
    implements_clause (plain children, one per listed interface).
    interface_declaration uses a separate extends_type_clause instead, whose
    repeated `type` field lists every extended interface -- TS interfaces
    only ever extend, never implement."""
    if node.type == "class_declaration":
        for child in node.children:
            if child.type != "class_heritage":
                continue
            results = []
            for sub in child.children:
                if sub.type == "extends_clause":
                    value = sub.child_by_field_name("value")
                    if value is not None:
                        results.append((_shorten_callee(_text(src, value)), "extends"))
                elif sub.type == "implements_clause":
                    for t in sub.children:
                        if t.type in ("type_identifier", "identifier", "generic_type", "nested_type_identifier"):
                            results.append((_shorten_callee(_text(src, t)), "implements"))
            return results
        return []
    if node.type == "interface_declaration":
        results = []
        for child in node.children:
            if child.type == "extends_type_clause":
                for t in child.children_by_field_name("type"):
                    results.append((_shorten_callee(_text(src, t)), "extends"))
        return results
    return []


# --- Java ---------------------------------------------------------------

def _java_import_target(node, src: bytes) -> list[str]:
    for child in node.children:
        if child.type in ("scoped_identifier", "identifier"):
            return [_text(src, child)]
    return []


def _java_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """class_declaration carries `superclass` (a `superclass` node wrapping
    one type_identifier) and/or `super_interfaces` (a `super_interfaces` node
    wrapping a `type_list`); interface_declaration carries `extends_interfaces`
    instead (same type_list shape) -- Java interfaces can extend several
    interfaces but never implement."""
    if node.type not in ("class_declaration", "interface_declaration"):
        return []
    results = []
    for child in node.children:
        if child.type == "superclass":
            for n in child.children:
                if n.type == "type_identifier":
                    results.append((_text(src, n), "extends"))
        elif child.type in ("super_interfaces", "extends_interfaces"):
            kind = "implements" if child.type == "super_interfaces" else "extends"
            for type_list in child.children:
                if type_list.type == "type_list":
                    for n in type_list.children:
                        if n.type == "type_identifier":
                            results.append((_text(src, n), kind))
    return results


# --- C# -------------------------------------------------------------

def _csharp_import_target(node, src: bytes) -> list[str]:
    for child in node.children:
        if child.type in ("qualified_name", "identifier"):
            return [_text(src, child)]
    return []


def _csharp_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """C#'s grammar doesn't distinguish a base class from an implemented
    interface in `base_list` (`class Foo : Bar, IBaz` parses Bar and IBaz
    identically) -- verified against real tree-sitter output. Disambiguated
    here by context, not guessed from syntax: an interface_declaration's
    base_list can only be other interfaces (always "extends"); a
    class_declaration's base_list's first entry is "extends" only if it
    doesn't look like an interface by C#'s near-universal `I<Upper...>`
    naming convention, everything else is "implements". This is a documented
    heuristic, not a guarantee -- a base class that breaks the naming
    convention would be mis-tagged "implements"."""
    if node.type not in ("class_declaration", "interface_declaration", "struct_declaration"):
        return []
    is_interface = node.type == "interface_declaration"
    results = []
    for child in node.children:
        if child.type != "base_list":
            continue
        names = [_text(src, c) for c in child.children if c.type in ("identifier", "qualified_name")]
        for i, name in enumerate(names):
            if is_interface:
                results.append((name, "extends"))
                continue
            looks_like_interface = bool(re.match(r"^I[A-Z]", name))
            kind = "extends" if (i == 0 and not looks_like_interface) else "implements"
            results.append((name, kind))
    return results


def _csharp_callee(node, src: bytes) -> str | None:
    """invocation_expression has no `function` field: the invoked
    expression is simply its first child -- a member_access_expression
    (take its last identifier) or a plain identifier."""
    if not node.children:
        return None
    target = node.children[0]
    if target.type == "member_access_expression" and target.children:
        last_id = target.children[-1]
        return _text(src, last_id)
    if target.type == "identifier":
        return _text(src, target)
    return None


# --- Go -------------------------------------------------------------

def _go_import_targets(node, src: bytes) -> list[str]:
    # node is an import_spec_list or a single import_spec (go allows both
    # `import "fmt"` and `import (...)`); collect every quoted path inside.
    targets = []
    for descendant in _iter(node):
        if descendant.type == "interpreted_string_literal":
            targets.append(_text(src, descendant).strip('"'))
    return targets


def _go_def_name(node, src: bytes) -> str | None:
    """type_spec covers every `type X = ...` declaration, including plain
    aliases (`type MyInt int`), and shares this extractor with
    function_declaration/method_declaration (one extractor per language,
    not per node type) -- so it must dispatch on node.type itself. Only
    surface a type_spec as a definition -- gated here, since returning None
    from extract_def_name is what actually suppresses the node, not
    extract_def_params -- when the type is really a struct."""
    if node.type != "type_spec":
        return _default_name(node, src)
    value = node.child_by_field_name("type")
    if value is None or value.type != "struct_type":
        return None
    name = node.child_by_field_name("name")
    return _text(src, name) if name is not None else None


def _go_def_params(node, src: bytes) -> str | None:
    if node.type == "type_spec":
        return None  # the struct body isn't a meaningful "signature"
    return _default_params(node, src)


def _iter(node):
    yield node
    for child in node.children:
        yield from _iter(child)


# --- PHP -------------------------------------------------------------

def _php_import_target(node, src: bytes) -> list[str]:
    for child in node.children:
        if child.type == "namespace_use_clause":
            return [_text(src, child)]
    return []


def _php_callee(node, src: bytes) -> str | None:
    if node.type in ("member_call_expression", "scoped_call_expression"):
        return _name_field_callee(node, src)
    return _default_callee(node, src)  # function_call_expression


def _php_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """`base_clause` (extends) is the same node type whether it belongs to a
    class (exactly one name) or an interface (one or more, PHP interfaces
    can extend several interfaces) -- both are "extends" either way.
    `class_interface_clause` (implements) only ever appears on a class."""
    if node.type not in ("class_declaration", "interface_declaration"):
        return []
    results = []
    for child in node.children:
        if child.type == "base_clause":
            for n in child.children:
                if n.type == "name":
                    results.append((_text(src, n), "extends"))
        elif child.type == "class_interface_clause":
            for n in child.children:
                if n.type == "name":
                    results.append((_text(src, n), "implements"))
    return results


# --- C / C++ -------------------------------------------------------------

def _c_include_target(node, src: bytes) -> list[str]:
    for child in node.children:
        if child.type == "string_literal":
            # A quoted include ("helper.h") is relative to this file's own
            # directory but, unlike JS, carries no leading "./" of its own --
            # `_resolve_relative_import` only attempts resolution when a
            # target starts with ".", so one is added here, at extraction,
            # where quoted-vs-angled is still known. Leave an existing
            # "./"/"../" prefix (e.g. "../common/helper.h") untouched.
            text = _text(src, child).strip('"')
            return [text if text.startswith(".") else f"./{text}"]
        if child.type == "system_lib_string":
            # <system.h>: never a local file. Left unprefixed so it simply
            # fails relative resolution and falls back to `external`.
            return [_text(src, child).strip("<>")]
    return []


def _c_def_name(node, src: bytes) -> str | None:
    """Shared across function_definition and struct_specifier/class_specifier
    (one extractor per language, not per node type), so it must dispatch:
    only function_definition nests its name inside a declarator -- structs
    and classes have a plain `name` field, same as the generic default."""
    if node.type != "function_definition":
        return _default_name(node, src)
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        return None
    inner = declarator.child_by_field_name("declarator")
    return _text(src, inner) if inner is not None else None


def _c_def_params(node, src: bytes) -> str | None:
    if node.type != "function_definition":
        return _default_params(node, src)
    declarator = node.child_by_field_name("declarator")
    if declarator is None:
        return None
    params = declarator.child_by_field_name("parameters")
    return _text(src, params) if params is not None else None


def _cpp_inherit(node, src: bytes) -> list[tuple[str, str]]:
    """`base_class_clause` lists type_identifiers interspersed with optional
    access_specifier nodes (public/private/protected) -- C++ has no
    `implements` concept (no interfaces), so every base is "extends"."""
    if node.type not in ("class_specifier", "struct_specifier"):
        return []
    results = []
    for child in node.children:
        if child.type == "base_class_clause":
            for n in child.children:
                if n.type == "type_identifier":
                    results.append((_text(src, n), "extends"))
    return results


# --- HTML / CSS (no functions/classes/calls; custom_parser bypasses the
# generic walker entirely) -------------------------------------------

def _parse_html(root_node, src: bytes) -> ParsedFile:
    parsed = ParsedFile()

    def walk(node):
        if node.type in ("element", "script_element"):
            start_tag = next((c for c in node.children if c.type == "start_tag"), None)
            if start_tag is not None:
                tag = None
                attr_value = None
                for child in start_tag.children:
                    if child.type == "tag_name" and tag is None:
                        tag = _text(src, child)
                    elif child.type == "attribute":
                        name = next((c for c in child.children if c.type == "attribute_name"), None)
                        if name is not None and _text(src, name) in ("src", "href"):
                            for descendant in _iter(child):
                                if descendant.type == "attribute_value":
                                    attr_value = _text(src, descendant)
                if tag in ("script", "link") and attr_value:
                    parsed.imports.append(attr_value)
        for child in node.children:
            walk(child)

    walk(root_node)
    return parsed


def _parse_css(root_node, src: bytes) -> ParsedFile:
    parsed = ParsedFile()

    def walk(node):
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "string_value":
                    parsed.imports.append(_text(src, child).strip('"\''))
                elif child.type == "call_expression":
                    for grandchild in _iter(child):
                        if grandchild.type == "string_value":
                            parsed.imports.append(_text(src, grandchild).strip('"\''))
        for child in node.children:
            walk(child)

    walk(root_node)
    return parsed


# --- SQL -------------------------------------------------------------

def _sql_def_name(node, src: bytes) -> str | None:
    for descendant in _iter(node):
        if descendant.type == "object_reference":
            for child in descendant.children:
                if child.type == "identifier":
                    return _text(src, child)
    return None


def _sql_invocation_callee(node, src: bytes) -> str | None:
    for child in node.children:
        if child.type == "object_reference":
            for grandchild in child.children:
                if grandchild.type == "identifier":
                    return _text(src, grandchild)
    return None


def _sql_table_reference(node, src: bytes) -> str | None:
    # node is a `relation` (FROM/JOIN target)
    for descendant in _iter(node):
        if descendant.type == "object_reference":
            for child in descendant.children:
                if child.type == "identifier":
                    return _text(src, child)
    return None


LANGUAGE_SPECS: dict[str, LanguageSpec] = {
    "python": LanguageSpec(
        extensions=[".py"],
        def_node_types={"function_definition": "function", "class_definition": "class"},
        import_node_types={"import_statement", "import_from_statement"},
        call_node_types={"call"},
        extract_import=_python_import_targets,
        extract_inherit=_python_inherit,
        import_resolution="dotted",
    ),
    "javascript": LanguageSpec(
        extensions=[".js", ".jsx"],
        def_node_types={"function_declaration": "function", "class_declaration": "class", "method_definition": "method"},
        import_node_types={"import_statement"},
        call_node_types={"call_expression"},
        extract_import=_js_import_target,
        extract_inherit=_js_inherit,
        import_resolution="relative",
    ),
    "typescript": LanguageSpec(
        extensions=[".ts"],
        def_node_types={"function_declaration": "function", "class_declaration": "class", "method_definition": "method",
                         "interface_declaration": "interface"},
        import_node_types={"import_statement"},
        call_node_types={"call_expression"},
        extract_import=_js_import_target,
        extract_inherit=_ts_inherit,
        import_resolution="relative",
    ),
    "tsx": LanguageSpec(
        extensions=[".tsx"],
        def_node_types={"function_declaration": "function", "class_declaration": "class", "method_definition": "method",
                         "interface_declaration": "interface"},
        import_node_types={"import_statement"},
        call_node_types={"call_expression"},
        extract_import=_js_import_target,
        extract_inherit=_ts_inherit,
        import_resolution="relative",
    ),
    "java": LanguageSpec(
        extensions=[".java"],
        def_node_types={"class_declaration": "class", "interface_declaration": "interface", "method_declaration": "method"},
        import_node_types={"import_declaration"},
        call_node_types={"method_invocation"},
        extract_import=_java_import_target,
        extract_callee=_name_field_callee,
        extract_inherit=_java_inherit,
        import_resolution="external",
    ),
    "csharp": LanguageSpec(
        extensions=[".cs"],
        def_node_types={"class_declaration": "class", "interface_declaration": "interface",
                         "struct_declaration": "class", "method_declaration": "method"},
        import_node_types={"using_directive"},
        call_node_types={"invocation_expression"},
        extract_import=_csharp_import_target,
        extract_callee=_csharp_callee,
        extract_inherit=_csharp_inherit,
        import_resolution="external",
    ),
    "go": LanguageSpec(
        extensions=[".go"],
        def_node_types={"function_declaration": "function", "method_declaration": "method", "type_spec": "class"},
        import_node_types={"import_declaration"},
        call_node_types={"call_expression"},
        extract_import=_go_import_targets,
        extract_def_name=_go_def_name,
        extract_def_params=_go_def_params,
        import_resolution="external",
    ),
    "php": LanguageSpec(
        extensions=[".php"],
        def_node_types={"class_declaration": "class", "interface_declaration": "interface",
                         "function_definition": "function", "method_declaration": "method"},
        import_node_types={"namespace_use_declaration"},
        call_node_types={"function_call_expression", "member_call_expression", "scoped_call_expression"},
        extract_import=_php_import_target,
        extract_callee=_php_callee,
        extract_inherit=_php_inherit,
        import_resolution="external",
    ),
    "c": LanguageSpec(
        extensions=[".c", ".h"],
        def_node_types={"function_definition": "function", "struct_specifier": "class"},
        import_node_types={"preproc_include"},
        call_node_types={"call_expression"},
        extract_import=_c_include_target,
        extract_def_name=_c_def_name,
        extract_def_params=_c_def_params,
        import_resolution="relative",
    ),
    "cpp": LanguageSpec(
        extensions=[".cpp", ".cc", ".cxx", ".hpp", ".hh"],
        def_node_types={"function_definition": "function", "class_specifier": "class", "struct_specifier": "class"},
        import_node_types={"preproc_include"},
        call_node_types={"call_expression"},
        extract_import=_c_include_target,
        extract_def_name=_c_def_name,
        extract_def_params=_c_def_params,
        extract_inherit=_cpp_inherit,
        import_resolution="relative",
    ),
    "html": LanguageSpec(
        extensions=[".html", ".htm"], def_node_types={}, custom_parser=_parse_html, import_resolution="relative"
    ),
    "css": LanguageSpec(
        extensions=[".css"], def_node_types={}, custom_parser=_parse_css, import_resolution="relative"
    ),
    "sql": LanguageSpec(
        extensions=[".sql"],
        def_node_types={"create_table": "table", "create_function": "function", "create_procedure": "function"},
        call_node_types={"invocation"},
        reference_node_types={"relation"},
        extract_def_name=_sql_def_name,
        extract_def_params=lambda node, src: None,
        extract_callee=_sql_invocation_callee,
        extract_reference=_sql_table_reference,
        attribute_top_level_to_file=True,
    ),
}

# Note on C#'s tree-sitter-language-pack key: it's "csharp", not "c_sharp".
# C and C++ share tree-sitter-c/-cpp's near-identical function_definition
# shape (name/params nested inside a function_declarator), so they reuse the
# same extractor functions despite being registered as separate languages.

LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ext: name for name, spec in LANGUAGE_SPECS.items() for ext in spec.extensions
}


def _is_within_root(path: Path, resolved_root: Path) -> bool:
    """Rejects a candidate (typically reached via a symlink) that resolves
    outside the repo root, without rejecting an in-repo symlink that
    resolves to another path still inside the repo."""
    try:
        return path.resolve().is_relative_to(resolved_root)
    except OSError:
        # Broken symlink or similar -- can't resolve, so can't verify the
        # boundary; treat as out of bounds rather than guessing.
        return False


def discover_files(root) -> list[Path]:
    root = Path(root)
    resolved_root = root.resolve()
    ignore_cfg = ignore_config.load_ignore_config(root)

    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True,
    )
    if result.returncode == 0:
        candidates = [root / line for line in result.stdout.splitlines() if line]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in VENDOR_DIR_NAMES and d not in ignore_cfg["exclude_dirs"]
            ]
            candidates.extend(Path(dirpath) / name for name in filenames)

    return sorted(
        p for p in candidates
        if p.suffix in LANGUAGE_BY_EXTENSION
        and p.is_file()
        and not any(part in VENDOR_DIR_NAMES for part in p.relative_to(root).parts)
        and not ignore_config.is_excluded(p.relative_to(root), ignore_cfg)
        and _is_within_root(p, resolved_root)
    )


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_file(path: Path, language: str) -> ParsedFile:
    spec = LANGUAGE_SPECS[language]
    parser = get_parser(language)
    src = path.read_bytes()
    tree = parser.parse(src)
    if spec.custom_parser is not None:
        return spec.custom_parser(tree.root_node, src)
    if language in ("javascript", "typescript", "tsx"):
        return _parse_js_like(tree.root_node, src, spec)
    return _generic_walk(tree.root_node, src, spec)


def _module_path_candidates(dotted: str) -> list[str]:
    """Suffixes of a dotted module path, longest first: 'a.b.c' -> ['a.b.c', 'b.c', 'c']."""
    parts = dotted.split(".")
    return [".".join(parts[i:]) for i in range(len(parts))]


def _resolve_python_import(target: str, module_index: dict[str, str]) -> str | None:
    for candidate in _module_path_candidates(target):
        if candidate in module_index:
            return module_index[candidate]
    return None


def _resolve_relative_import(target: str, importing_file: Path, known_files: set[Path]) -> Path | None:
    """Shared by JS/TS (`./foo`) and C/C++ (`#include "foo.h"`). A C/C++
    `<system.h>` include is fed through the same path unchanged -- it simply
    never matches a real file and falls back to `external`, so there's no
    need to special-case angle-bracket vs quoted syntax here."""
    if not target.startswith("."):
        return None
    base = (importing_file.parent / target).resolve()
    # `base` itself first: HTML/CSS targets (`./styles.css`) already carry
    # their real extension, so the unmodified path must be tried as-is --
    # `with_suffix("")` below would otherwise strip it and never match.
    # The suffix variants exist for JS/TS's common extensionless `./foo`.
    candidates = [base] + [
        base.with_suffix(ext) for ext in ("", ".ts", ".tsx", ".js", ".jsx", ".h", ".hpp", ".c", ".cpp")
    ]
    candidates += [base / f"index{ext}" for ext in (".ts", ".tsx", ".js", ".jsx")]
    for candidate in candidates:
        if candidate in known_files:
            return candidate
    return None


_TEST_DIR_MARKERS = {"test", "tests", "__tests__", "spec", "specs"}


def is_test_file(rel_path: str) -> bool:
    """Best-effort, language-agnostic heuristic: a path component named like
    a test directory, or a filename stem containing "test"/"spec" as a whole
    word (snake_case: test_foo.py, foo_test.py, foo.spec.ts) or as a
    PascalCase suffix (FooTest.java, FooTests.cs). A heuristic, not a
    guarantee -- same honesty as every other best-effort rule in this module.
    """
    p = Path(rel_path)
    parts_lower = {part.lower() for part in p.parts[:-1]}
    if parts_lower & _TEST_DIR_MARKERS:
        return True
    stem = p.stem
    if re.search(r"(?i)(^|[_.\-])(tests?|specs?)($|[_.\-])", stem):
        return True
    return bool(re.search(r"(Test|Tests)$", stem))


@dataclass
class ScanSummary:
    files_scanned: int
    files_unchanged: int
    nodes: int
    edges: int


class _GraphStore:
    """Thin wrapper over the nodes/edges tables for one scan."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def node_id(self, file: str, symbol: str | None) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM nodes WHERE file = ? AND symbol IS ?", (file, symbol)
        ).fetchone()
        return row["id"] if row else None

    def upsert_node(self, kind, file, symbol=None, language=None, content_hash_=None,
                     line_start=None, line_end=None, signature=None, docstring=None,
                     body_hash_=None, now=None, scanned_branch_=None) -> int:
        # content_hash uses COALESCE(new, existing): a cross-file import
        # resolution upserts the *target* file's node just to get its id,
        # without re-parsing it, so it never has a hash to offer. A plain
        # overwrite would null out that file's real hash and silently defeat
        # incremental rescanning for it from then on. body_hash follows the
        # same COALESCE pattern for the same reason. first_seen_at is set
        # only on a genuine first INSERT (DO UPDATE keeps the existing
        # value); last_seen_at always advances to `now`.
        now = now or datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO nodes (kind, file, symbol, language, content_hash, line_start, line_end,
                                signature, docstring, body_hash, first_seen_at, last_seen_at, scanned_branch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file, COALESCE(symbol, '')) DO UPDATE SET
                kind=excluded.kind, language=excluded.language,
                content_hash=COALESCE(excluded.content_hash, nodes.content_hash),
                line_start=excluded.line_start, line_end=excluded.line_end,
                signature=excluded.signature, docstring=excluded.docstring,
                body_hash=COALESCE(excluded.body_hash, nodes.body_hash),
                last_seen_at=excluded.last_seen_at,
                scanned_branch=COALESCE(excluded.scanned_branch, nodes.scanned_branch)
            """,
            (kind, file, symbol, language, content_hash_, line_start, line_end, signature, docstring,
             body_hash_, now, now, scanned_branch_),
        )
        return self.node_id(file, symbol)

    def fetch_symbol_rows(self, rel_path: str) -> list[dict]:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT * FROM nodes WHERE file = ? AND symbol IS NOT NULL", (rel_path,)
            )
        ]

    def snapshot_node_history(self, row: dict, scan_id: str, superseded_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO node_history
                (node_id, kind, file, symbol, language, content_hash, body_hash,
                 line_start, line_end, signature, docstring, superseded_at, scan_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"], row["kind"], row["file"], row["symbol"], row["language"],
                row["content_hash"], row.get("body_hash"), row["line_start"], row["line_end"],
                row["signature"], row["docstring"], superseded_at, scan_id,
            ),
        )

    def add_rename_candidate(self, old_file: str, old_symbol: str | None, new_node_id: int,
                              confidence: str, detected_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO node_rename_candidates
                (old_node_file, old_node_symbol, new_node_id, basis, confidence, detected_at, resolved)
            VALUES (?, ?, ?, 'body_hash_match', ?, ?, 0)
            """,
            (old_file, old_symbol, new_node_id, confidence, detected_at),
        )

    def external_node(self, name: str) -> int:
        existing = self.conn.execute(
            "SELECT id FROM nodes WHERE kind = 'external' AND file = ?", (name,)
        ).fetchone()
        if existing:
            return existing["id"]
        return self.upsert_node("external", name)

    def add_edge(self, src_id: int, dst_id: int, kind: str) -> None:
        self.conn.execute(
            "INSERT INTO edges (src_node_id, dst_node_id, kind) VALUES (?, ?, ?)", (src_id, dst_id, kind)
        )

    def delete_file(self, rel_path: str, scan_id: str, now: str) -> list[dict]:
        """Fully remove a file (it no longer exists) and every edge touching
        any of its nodes, including other files' now-dangling imports of it.
        Every symbol row is snapshotted into node_history before deletion
        (not the file-level row itself -- a whole-file disappearance isn't
        currently fed into rename-candidate matching, only symbol-level
        moves are) and returned so the caller can fold them into this scan's
        disappeared-symbol pool for rename-candidate matching -- a file
        rename/move surfaces as its old symbols matching a new file's new
        ones, cross-file (low confidence), the same mechanism as an in-file
        rename.
        """
        symbol_rows = self.fetch_symbol_rows(rel_path)
        for row in symbol_rows:
            self.snapshot_node_history(row, scan_id, now)

        node_ids = [r["id"] for r in self.conn.execute("SELECT id FROM nodes WHERE file = ?", (rel_path,))]
        for node_id in node_ids:
            self.conn.execute("DELETE FROM edges WHERE src_node_id = ? OR dst_node_id = ?", (node_id, node_id))
        self.conn.execute("DELETE FROM nodes WHERE file = ?", (rel_path,))
        return symbol_rows

    def clear_file_for_rescan(self, rel_path: str) -> None:
        """Drop this file's symbol nodes and its own outgoing edges ahead of
        re-parsing it, but keep the file node's row (and id) stable: other
        files' `imports` edges may already point at it by id, and an
        unchanged file is never revisited to re-link them."""
        symbol_ids = [
            r["id"] for r in self.conn.execute(
                "SELECT id FROM nodes WHERE file = ? AND symbol IS NOT NULL", (rel_path,)
            )
        ]
        for sid in symbol_ids:
            self.conn.execute("DELETE FROM edges WHERE src_node_id = ? OR dst_node_id = ?", (sid, sid))
        if symbol_ids:
            self.conn.execute("DELETE FROM nodes WHERE file = ? AND symbol IS NOT NULL", (rel_path,))

        file_id = self.node_id(rel_path, None)
        if file_id is not None:
            self.conn.execute("DELETE FROM edges WHERE src_node_id = ?", (file_id,))


def scan(root) -> ScanSummary:
    root = Path(root)
    storage.init_store(root)

    conn = storage._connect(root)
    try:
        store = _GraphStore(conn)
        scan_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        current_branch = git_utils.current_branch(root)  # None outside a git repo/detached HEAD
        # Symbol rows that disappeared this scan (by rename/removal within a
        # changed file, or because their whole file was deleted/moved),
        # matched against `newly_inserted` after pass 1 to surface
        # rename candidates -- advisory only, never auto-merged.
        disappeared: list[dict] = []
        newly_inserted: list[dict] = []  # {node_id, file, symbol, body_hash}

        files = discover_files(root)
        known_files = set(files)
        rel_of = {f: f.relative_to(root).as_posix() for f in files}
        current_rel_paths = set(rel_of.values())

        existing_hashes = {
            row["file"]: row["content_hash"]
            for row in conn.execute("SELECT file, content_hash FROM nodes WHERE kind = 'file'")
        }
        for stale_rel in set(existing_hashes) - current_rel_paths:
            disappeared.extend(store.delete_file(stale_rel, scan_id, now))

        # Dotted-module-path index for Python import resolution, built from
        # every discovered file regardless of whether it changed this scan.
        module_index: dict[str, str] = {}
        for path, rel in rel_of.items():
            if LANGUAGE_BY_EXTENSION.get(path.suffix) == "python":
                dotted = rel[: -len(path.suffix)].replace("/", ".")
                for candidate in _module_path_candidates(dotted):
                    module_index.setdefault(candidate, rel)

        files_unchanged = 0
        changed: list[tuple[Path, str, str, str]] = []  # (path, rel, language, content_hash)
        for path in files:
            rel = rel_of[path]
            language = LANGUAGE_BY_EXTENSION[path.suffix]
            h = content_hash(path)
            if existing_hashes.get(rel) == h:
                files_unchanged += 1
                continue
            changed.append((path, rel, language, h))

        # Two passes, not one: pass 1 inserts every changed file's own node
        # and definitions (including SQL `table` nodes); pass 2 resolves
        # imports/calls/references against them. A single pass would make
        # resolution depend on iteration order within this same batch --
        # e.g. "query.sql" sorts before "schema.sql", so if both changed in
        # the same scan, query.sql's reference to a table schema.sql defines
        # would wrongly fall back to `external` just because its file name
        # happens to come first alphabetically.
        parsed_by_file: dict[str, tuple[Path, str, ParsedFile, int, dict[str, int]]] = {}
        for path, rel, language, h in changed:
            try:
                parsed = parse_file(path, language)
            except Exception:
                continue

            # Snapshot the file's current symbol rows *before* clearing them,
            # keyed by name, so a same-named symbol's body change (or a
            # vanished name, possibly a rename) can be detected below --
            # fetched here, not reused from `existing_hashes`, since that
            # index only covers file-level hashes, not per-symbol ones.
            old_symbols_by_name = {r["symbol"]: r for r in store.fetch_symbol_rows(rel)}

            store.clear_file_for_rescan(rel)
            file_id = store.upsert_node(
                "file", rel, language=language, content_hash_=h, now=now, scanned_branch_=current_branch
            )

            symbol_ids: dict[str, int] = {}
            for d in parsed.definitions:
                old = old_symbols_by_name.pop(d.name, None)
                if old is not None and old.get("body_hash") and old["body_hash"] != d.body_hash:
                    store.snapshot_node_history(old, scan_id, now)

                sym_id = store.upsert_node(
                    d.kind, rel, d.name, language=language,
                    line_start=d.line_start, line_end=d.line_end,
                    signature=d.signature, docstring=d.docstring,
                    body_hash_=d.body_hash, now=now,
                )
                symbol_ids[d.name] = sym_id
                store.add_edge(file_id, sym_id, "defines")
                if d.body_hash:
                    newly_inserted.append({"node_id": sym_id, "file": rel, "symbol": d.name, "body_hash": d.body_hash})

            # Whatever's left under its old name never reappeared in this
            # file's fresh parse -- either genuinely removed, or renamed/
            # moved, which the post-pass below checks for via body_hash.
            for old in old_symbols_by_name.values():
                store.snapshot_node_history(old, scan_id, now)
                disappeared.append(old)

            parsed_by_file[rel] = (path, language, parsed, file_id, symbol_ids)

        for old in disappeared:
            old_body_hash = old.get("body_hash")
            if not old_body_hash:
                continue  # nothing to match against -- never guess a rename without evidence
            for candidate in newly_inserted:
                if candidate["body_hash"] == old_body_hash:
                    confidence = "high" if candidate["file"] == old["file"] else "low"
                    store.add_rename_candidate(old["file"], old["symbol"], candidate["node_id"], confidence, now)

        for rel, (path, language, parsed, file_id, symbol_ids) in parsed_by_file.items():
            resolution = LANGUAGE_SPECS[language].import_resolution
            for target in parsed.imports:
                if resolution == "dotted":
                    resolved_rel = _resolve_python_import(target, module_index)
                elif resolution == "relative":
                    resolved = _resolve_relative_import(target, path, known_files)
                    resolved_rel = resolved.relative_to(root).as_posix() if resolved else None
                else:  # "external": this language's own package/classpath
                    resolved_rel = None  # scheme isn't path-based -- never attempt local resolution

                if resolved_rel and resolved_rel != rel:
                    dst_id = store.upsert_node("file", resolved_rel, language=LANGUAGE_BY_EXTENSION.get(Path(resolved_rel).suffix))
                elif resolved_rel is None:
                    dst_id = store.external_node(target)
                else:
                    dst_id = None  # self-import; nothing to link
                if dst_id is not None:
                    store.add_edge(file_id, dst_id, "imports")

            file_is_test = is_test_file(rel)
            for caller_name, callee_name in parsed.calls:
                caller_id = file_id if caller_name is None else symbol_ids.get(caller_name)
                callee_id = symbol_ids.get(callee_name)
                if caller_id is not None and callee_id is not None and caller_id != callee_id:
                    store.add_edge(caller_id, callee_id, "calls")
                    if file_is_test:
                        # Double-tagged, not merged into "calls" itself: a
                        # TESTS edge is a genuinely different relationship
                        # (this call exists to verify behavior, not to
                        # implement it), and callers that want "what verifies
                        # this" shouldn't have to also filter out every
                        # ordinary same-file call. Same same-file-only
                        # resolution limit as "calls" itself applies here --
                        # the common case of a test file importing and
                        # calling the module under test isn't captured,
                        # since that's a cross-file call this module never
                        # resolves at all (see the module docstring).
                        store.add_edge(caller_id, callee_id, "tests")

            for class_name, base_name, rel_kind in parsed.inherits:
                # Same-file resolution only, same honesty as "calls" above:
                # a base class/interface defined in another file is simply
                # not linked, not guessed at via an external node.
                src_id = symbol_ids.get(class_name)
                dst_id = symbol_ids.get(base_name)
                if src_id is not None and dst_id is not None and src_id != dst_id:
                    store.add_edge(src_id, dst_id, rel_kind)

            for caller_name, table_name in parsed.references:
                # SQL only (every other language's `references` list is
                # always empty): resolve against any table already known in
                # the store. The two-pass structure above means every table
                # defined by *any* file changing in this same scan is
                # already inserted by the time references are resolved, so
                # this only still falls back to `external` when the table
                # lives in a file that didn't change this run -- the same
                # accepted, self-healing-on-a-later-scan tradeoff as a
                # brand-new file's incoming imports not retroactively
                # relinking other, unchanged files.
                caller_id = file_id if caller_name is None else symbol_ids.get(caller_name)
                table_row = conn.execute(
                    "SELECT id FROM nodes WHERE kind = 'table' AND symbol = ?", (table_name,)
                ).fetchone()
                target_id = table_row["id"] if table_row else store.external_node(table_name)
                if caller_id is not None and target_id is not None and caller_id != target_id:
                    store.add_edge(caller_id, target_id, "references")

        conn.commit()
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return ScanSummary(
            files_scanned=len(changed), files_unchanged=files_unchanged,
            nodes=node_count, edges=edge_count,
        )
    finally:
        conn.close()


def branch_staleness(root) -> str | None:
    """One-line advisory, never blocking: warns when the knowledge graph
    includes file nodes scanned under a branch other than the one currently
    checked out. Returns None when there's nothing to warn about -- not a
    git repo/detached HEAD, or every scanned file matches the current branch
    (including when scanned_branch itself is always NULL, e.g. no file has
    been (re)scanned since this feature shipped).
    """
    current = git_utils.current_branch(root)
    if current is None:
        return None
    conn = storage._connect(root)
    try:
        rows = conn.execute(
            "SELECT DISTINCT scanned_branch FROM nodes WHERE kind = 'file' AND scanned_branch IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    stale = {r["scanned_branch"] for r in rows} - {current}
    if not stale:
        return None
    return (
        f"devmemory: knowledge graph includes files scanned under other branch(es) "
        f"({', '.join(sorted(stale))}) while on {current!r} -- consider rescanning."
    )


def stats(root) -> dict:
    conn = storage._connect(root)
    try:
        nodes = {r["kind"]: r["c"] for r in conn.execute("SELECT kind, COUNT(*) AS c FROM nodes GROUP BY kind")}
        edges = {r["kind"]: r["c"] for r in conn.execute("SELECT kind, COUNT(*) AS c FROM edges GROUP BY kind")}
        return {"nodes": nodes, "edges": edges}
    finally:
        conn.close()


def _mermaid_id(file: str, symbol: str | None) -> str:
    raw = f"{file}::{symbol}" if symbol else file
    return "n" + re.sub(r"[^A-Za-z0-9_]", "_", raw)


def _decision_summary(root, file: str, symbol: str | None) -> tuple[int, str | None]:
    """How many devmemory decision records exist for this node, and the most
    recent one's summary -- the cross-reference between the structural graph
    and devmemory's own decision-record memory, keyed on the same file+symbol."""
    # storage.query_decisions treats symbol=None as "don't filter on symbol",
    # so for a file-level node (symbol=None) this already returns every
    # decision recorded anywhere in that file, not just an exact-NULL match.
    records = storage.query_decisions(root, file=file, symbol=symbol)
    if not records:
        return 0, None
    return len(records), records[-1]["summary"]


def render_mermaid(root, detail: str = "files", file: str | None = None) -> str:
    """Render the graph as Mermaid and write it to .devmemory/knowledge/graph.mmd.

    detail="files" (default): file + external-package nodes, import edges --
    small enough to stay legible for an unfamiliar codebase. detail="symbols"
    (requires `file`): that one file's function/class/method nodes and their
    defines/calls edges. Nodes with recorded devmemory decisions are styled
    distinctly and labeled with their decision count, so the graph itself
    shows where context already exists -- not just structure.
    """
    if detail not in ("files", "symbols"):
        raise ValueError(f"unknown detail level: {detail!r}")
    if detail == "symbols" and not file:
        raise ValueError('render_mermaid(detail="symbols") requires file=<path>')

    root = Path(root)
    conn = storage._connect(root)
    try:
        if detail == "files":
            nodes = conn.execute("SELECT * FROM nodes WHERE kind IN ('file', 'external')").fetchall()
            edges = conn.execute(
                """
                SELECT sn.file AS src_file, sn.symbol AS src_symbol,
                       dn.file AS dst_file, dn.symbol AS dst_symbol, e.kind
                FROM edges e
                JOIN nodes sn ON sn.id = e.src_node_id
                JOIN nodes dn ON dn.id = e.dst_node_id
                WHERE e.kind = 'imports'
                """
            ).fetchall()
        else:
            nodes = conn.execute(
                "SELECT * FROM nodes WHERE file = ? AND symbol IS NOT NULL", (file,)
            ).fetchall()
            edges = conn.execute(
                """
                SELECT sn.file AS src_file, sn.symbol AS src_symbol,
                       dn.file AS dst_file, dn.symbol AS dst_symbol, e.kind
                FROM edges e
                JOIN nodes sn ON sn.id = e.src_node_id
                JOIN nodes dn ON dn.id = e.dst_node_id
                WHERE e.kind IN ('calls', 'references', 'extends', 'implements', 'tests')
                      AND (sn.file = ? OR dn.file = ?)
                """, (file, file),
            ).fetchall()

        lines = ["graph TD", "    classDef hasMemory fill:#fef3c7,stroke:#d97706,stroke-width:2px;"]
        styled: list[str] = []
        for n in nodes:
            node_id = _mermaid_id(n["file"], n["symbol"])
            label = n["symbol"] or n["file"]
            count, _ = _decision_summary(root, n["file"], n["symbol"])
            if count:
                label = f"{label} ({count} memo)"
                styled.append(node_id)
            safe_label = label.replace('"', "'")
            lines.append(f'    {node_id}["{safe_label}"]')
        arrows = {"calls": "-.->", "references": "==>", "extends": "--|>", "implements": "-.|>", "tests": "-->"}
        for e in edges:
            src_id = _mermaid_id(e["src_file"], e["src_symbol"])
            dst_id = _mermaid_id(e["dst_file"], e["dst_symbol"])
            arrow = arrows.get(e["kind"], "-->")
            lines.append(f"    {src_id} {arrow} {dst_id}")
        for node_id in styled:
            lines.append(f"    class {node_id} hasMemory")

        diagram = "\n".join(lines) + "\n"
        out_path = get_mermaid_path(root)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(diagram, encoding="utf-8")
        return diagram
    finally:
        conn.close()


def node_history(root, file: str, symbol: str | None = None) -> list[dict]:
    """Prior snapshots of a symbol's row, most recent first -- taken only
    when its body actually changed across a rescan, never on every scan."""
    conn = storage._connect(root)
    try:
        rows = conn.execute(
            "SELECT * FROM node_history WHERE file = ? AND symbol IS ? ORDER BY superseded_at DESC",
            (file, symbol),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def rename_candidates(root, resolved: bool | None = None) -> list[dict]:
    """Same-body-hash matches between a symbol that disappeared and one that
    newly appeared -- advisory only, never auto-merged into a single
    identity. `resolved=None` (default) returns every candidate regardless of
    whether a human has acted on it; pass True/False to filter.
    """
    conn = storage._connect(root)
    try:
        sql = "SELECT * FROM node_rename_candidates"
        params: list = []
        if resolved is not None:
            sql += " WHERE resolved = ?"
            params.append(1 if resolved else 0)
        sql += " ORDER BY detected_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_rename_candidate_resolved(root, candidate_id: int) -> None:
    conn = storage._connect(root)
    try:
        conn.execute("UPDATE node_rename_candidates SET resolved = 1 WHERE id = ?", (candidate_id,))
        conn.commit()
    finally:
        conn.close()


def inspect(root, file: str, symbol: str | None = None) -> dict:
    """Full metadata + context for one node: its own attributes, its graph
    edges in both directions, and any devmemory decision records tied to it --
    the detail view the overview diagram deliberately doesn't try to cram in."""
    root = Path(root)
    conn = storage._connect(root)
    try:
        node = conn.execute(
            "SELECT * FROM nodes WHERE file = ? AND symbol IS ?", (file, symbol)
        ).fetchone()
        if node is None:
            return {"found": False, "file": file, "symbol": symbol}

        outgoing = conn.execute(
            """
            SELECT dn.file, dn.symbol, dn.kind, e.kind AS edge_kind
            FROM edges e JOIN nodes dn ON dn.id = e.dst_node_id
            WHERE e.src_node_id = ?
            """, (node["id"],),
        ).fetchall()
        incoming = conn.execute(
            """
            SELECT sn.file, sn.symbol, sn.kind, e.kind AS edge_kind
            FROM edges e JOIN nodes sn ON sn.id = e.src_node_id
            WHERE e.dst_node_id = ?
            """, (node["id"],),
        ).fetchall()

        return {
            "found": True,
            "node": dict(node),
            "outgoing": [dict(r) for r in outgoing],
            "incoming": [dict(r) for r in incoming],
            "decisions": storage.query_decisions(root, file=file, symbol=symbol),
        }
    finally:
        conn.close()


def impact(root, file: str, symbol: str, max_depth: int = 3) -> dict:
    """What would be affected by changing this symbol -- the transitive
    closure of its callers (and, for SQL, referencers), not just inspect()'s
    one-hop incoming edges. Bounded at max_depth, per the same precision-
    over-an-unbounded-dump reasoning as the rest of the graph's design: an
    impact report that tries to list everything, arbitrarily deep, in a
    heavily interconnected codebase stops being useful at exactly the point
    it stops being small.

    Each impacted node carries its own devmemory decision count, so prior
    rationale for code this change would touch is visible without a second
    lookup. Files that merely `import` this symbol's file -- which may or
    may not actually use this specific symbol, the graph can't tell without
    real type resolution -- are reported separately and explicitly labeled
    lower-confidence, never merged into the precise call-chain levels.

    A thin wrapper over retrieval.traverse(profile_name="change_impact") --
    that profile's config was chosen to exactly match this function's
    pre-existing behavior, and this keeps that exact output shape (just
    found/file/symbol/levels/importing_files/node_ids, not the newer
    profile-specific fields) for callers written against it.
    """
    result = retrieval.traverse(root, file, symbol, profile_name="change_impact", max_depth_override=max_depth)
    if not result["found"]:
        return {"found": False, "file": file, "symbol": symbol}
    return {
        "found": True,
        "file": file,
        "symbol": symbol,
        "levels": result["levels"],
        "importing_files": result["importing_files"],
        "node_ids": result["node_ids"],
    }
