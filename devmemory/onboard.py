"""Retrospective onboarding: discovers existing AI-instruction and
architectural-context files in a repo devmemory is being introduced into
after the fact, and stores them as `context_records` linked (where a
reliable link exists) to the code-structure knowledge graph.

Discovery is pattern-based, not exhaustive-NLP: a fixed set of filename/path
conventions (README, AGENTS.md, CLAUDE.md, `.cursor/rules/`,
`.github/copilot-instructions.md` and `.github/instructions/`, ADR-looking
directories, OpenAPI/Swagger specs, migration directories). Anything not
matching a recognized pattern is left alone -- this never treats "every file
in the repo" as a context source.

Linking (`link_to_nodes`) is deliberately conservative: a context record is
linked to a node only via an *exact* match -- a known file path appearing in
its content, or a symbol name that is *globally unique* across the whole
graph. An ambiguous or unmatched record simply stays at its own
directory/project scope rather than guessing. This mirrors the same honesty
already applied throughout knowledge.py's own parsing limits.
"""

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import git_utils, ignore_config, storage
from .knowledge import VENDOR_DIR_NAMES

MAX_CONTENT_CHARS = 20000

README_RE = re.compile(r"^readme(\.\w+)?$", re.IGNORECASE)
OPENAPI_RE = re.compile(r"^(openapi|swagger)[\w.\-]*\.(ya?ml|json)$", re.IGNORECASE)
ADR_DIR_MARKERS = {"adr", "adrs"}
MIGRATION_DIR_MARKERS = {"migrations", "migration"}


@dataclass
class OnboardReport:
    indexed: int = 0
    skipped_unchanged: int = 0
    skipped_unrecognized: int = 0
    failed: int = 0
    linked: int = 0


def _dir_scope(rel: Path) -> str | None:
    parent = rel.parent.as_posix()
    return None if parent == "." else parent


def _list_all_files(root) -> list[Path]:
    """Every file in the repo, respecting .gitignore (or the hardcoded
    vendor-dir list + .devmemory/ignore.json outside a git repo) -- unlike
    knowledge.discover_files, not filtered to code extensions, since
    onboarding looks for docs/config files, not source code."""
    root = Path(root)
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
                d for d in dirnames if d not in VENDOR_DIR_NAMES and d not in ignore_cfg["exclude_dirs"]
            ]
            candidates.extend(Path(dirpath) / name for name in filenames)

    return [
        p for p in candidates
        if p.is_file()
        and not any(part in VENDOR_DIR_NAMES for part in p.relative_to(root).parts)
        and not ignore_config.is_excluded(p.relative_to(root), ignore_cfg)
    ]


def classify_file(root, path: Path) -> dict | None:
    """Returns {"kind", "scope_path"} for a recognized context source, or
    None. Scope is always derived directly from the path itself -- a nested
    instruction file is never promoted to project-wide scope."""
    rel = path.relative_to(root)
    parts = rel.parts
    name = rel.name

    if README_RE.match(name):
        return {"kind": "readme", "scope_path": _dir_scope(rel)}
    if name == "AGENTS.md":
        return {"kind": "agents_md", "scope_path": _dir_scope(rel)}
    if name == "CLAUDE.md":
        return {"kind": "claude_md", "scope_path": _dir_scope(rel)}
    if ".cursor" in parts and "rules" in parts and name.endswith((".mdc", ".md")):
        idx = parts.index(".cursor")
        scope_parts = parts[:idx]
        return {"kind": "cursor_rule", "scope_path": "/".join(scope_parts) or None}
    if rel.as_posix() == ".github/copilot-instructions.md":
        return {"kind": "copilot_instructions", "scope_path": None}
    if ".github" in parts and "instructions" in parts and name.endswith(".md"):
        # Copilot's per-path `applyTo` frontmatter isn't parsed here -- this
        # always lands at project scope, a documented limitation rather than
        # a guessed one.
        return {"kind": "copilot_instructions", "scope_path": None}
    if any(p.lower() in ADR_DIR_MARKERS for p in parts[:-1]) and name.lower().endswith((".md", ".txt")):
        return {"kind": "adr", "scope_path": _dir_scope(rel)}
    if OPENAPI_RE.match(name):
        return {"kind": "openapi_spec", "scope_path": _dir_scope(rel)}
    if any(p.lower() in MIGRATION_DIR_MARKERS for p in parts[:-1]):
        return {"kind": "db_migration", "scope_path": _dir_scope(rel)}
    return None


def discover_and_classify(root) -> list[dict]:
    root = Path(root)
    found = []
    for path in _list_all_files(root):
        classified = classify_file(root, path)
        if classified is not None:
            found.append({**classified, "path": path, "source_path": path.relative_to(root).as_posix()})
    return found


def _read_content(kind: str, path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if kind == "db_migration":
        # Summarized by filename/revision only, not the full SQL -- most
        # migration files are mechanical and bulky; the filename (often
        # itself a revision id/timestamp) plus a first-line hint is enough
        # to link it, and far cheaper to store.
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        content = path.name
        if first_line:
            content += f" -- {first_line[:200]}"
        return content
    return text[:MAX_CONTENT_CHARS]


def link_to_nodes(root) -> int:
    """Links each context record to every node it names exactly: a known
    file path appearing verbatim in its content, or a symbol name that is
    globally unique across the whole graph (a repeated name like "Config" is
    never linked -- it's genuinely ambiguous which one is meant). Idempotent:
    an already-linked (node, record) pair is never re-linked. Returns the
    number of new links created this call.
    """
    conn = storage._connect(root)
    try:
        nodes = conn.execute("SELECT id, file, symbol FROM nodes").fetchall()
        records = conn.execute("SELECT id, content FROM context_records").fetchall()
        existing_pairs = {
            (row["src_ref"], row["dst_ref"])
            for row in conn.execute(
                "SELECT src_ref, dst_ref FROM decision_links "
                "WHERE kind = 'DOCUMENTED_BY' AND src_kind = 'node' AND dst_kind = 'context_record'"
            )
        }
    finally:
        conn.close()

    file_node_id: dict[str, int] = {}
    symbol_name_hits: dict[str, list[int]] = {}
    for n in nodes:
        if n["symbol"] is None:
            file_node_id[n["file"]] = n["id"]
        else:
            symbol_name_hits.setdefault(n["symbol"], []).append(n["id"])
    unique_symbol_node = {name: ids[0] for name, ids in symbol_name_hits.items() if len(ids) == 1}

    linked = 0
    for rec in records:
        content = rec["content"]
        targets: set[int] = set()
        for file_path, node_id in file_node_id.items():
            if file_path and file_path in content:
                targets.add(node_id)
        for name, node_id in unique_symbol_node.items():
            if re.search(rf"\b{re.escape(name)}\b", content):
                targets.add(node_id)

        for node_id in targets:
            pair = (str(node_id), str(rec["id"]))
            if pair in existing_pairs:
                continue
            storage.add_decision_link(
                root, "node", str(node_id), "context_record", str(rec["id"]), "DOCUMENTED_BY",
                origin="model-inference", confidence=0.6,
            )
            linked += 1

    return linked


def onboard(root, dry_run: bool = False) -> OnboardReport:
    root = Path(root)
    storage.init_store(root)
    report = OnboardReport()

    existing_hashes = storage.get_context_record_hashes(root)
    branch = git_utils.current_branch(root)

    for candidate in discover_and_classify(root):
        try:
            content = _read_content(candidate["kind"], candidate["path"])
        except UnicodeDecodeError:
            report.skipped_unrecognized += 1
            continue
        except OSError:
            report.failed += 1
            continue

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if existing_hashes.get(candidate["source_path"]) == content_hash:
            report.skipped_unchanged += 1
            continue

        if not dry_run:
            storage.upsert_context_record(
                root, kind=candidate["kind"], source_path=candidate["source_path"],
                title=candidate["path"].name, content=content, scope_path=candidate["scope_path"],
                branch=branch, content_hash=content_hash,
            )
        report.indexed += 1

    if not dry_run:
        report.linked = link_to_nodes(root)

    return report
