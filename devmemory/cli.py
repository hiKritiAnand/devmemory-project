"""devmemory CLI: init, record, query, log, extract, check-session, install <platform>."""

import argparse
import importlib.metadata
import json
import re
import shlex
import sqlite3
import sys
from pathlib import Path

from . import compiler as compiler_module
from . import conflicts as conflicts_module
from . import extract, git_utils, knowledge, migrations, onboard as onboard_module
from . import profiles as profiles_module
from . import retrieval, session_check, storage
from .adapters import claude as claude_adapter
from .adapters import cursor as cursor_adapter
from .adapters import githook as githook_adapter

PLATFORM_ADAPTERS = {
    "claude": claude_adapter,
    "cursor": cursor_adapter,
    "git": githook_adapter,
}


def _format_decision_line(record: dict) -> str:
    target = record["file"]
    if record.get("symbol"):
        target += f"::{record['symbol']}"
    line = f"#{record['id']} [{record['created_at']}] {target}"
    status = record.get("status")
    if status and status != "accepted":
        line += f" ({status}"
        if record.get("superseded_by"):
            line += f", superseded by #{record['superseded_by']}"
        line += ")"
    return line


def cmd_init(args) -> None:
    root = Path(args.path).resolve()
    db_path = storage.init_store(root)
    print(f"Initialized devmemory store at {db_path}")


def cmd_record(args) -> None:
    root = Path(args.path).resolve()
    tags = args.tags.split(",") if args.tags else None
    try:
        record = storage.add_decision(
            root,
            file=args.file,
            summary=args.summary,
            symbol=args.symbol,
            why=args.why,
            rejected=args.rejected,
            tags=tags,
            session_ref=args.session,
            author=args.author,
            status=args.status,
            alternatives=args.alternatives,
            consequences=args.consequences,
            scope=args.scope,
            owner=args.owner,
            global_=args.global_,
        )
    except ValueError as e:
        print(f"devmemory record failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Recorded decision #{record['id']} for {record['file']}")

    if args.supersedes:
        try:
            storage.supersede_decision(root, args.supersedes, record["id"], changed_by=args.author)
        except ValueError as e:
            print(f"devmemory record: could not supersede #{args.supersedes}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Marked decision #{args.supersedes} as superseded by #{record['id']}")

    # Conflict detection never blocks the write above -- it only warns, after
    # the fact, about conflicts that now involve this new decision.
    found = conflicts_module.detect_conflicts(root, args.file, args.symbol)
    for c in found:
        if record["id"] not in (c.decision_a["id"], c.decision_b["id"]):
            continue
        other = c.decision_b if c.decision_a["id"] == record["id"] else c.decision_a
        print(
            f"WARNING: possible conflict ({c.rule}, {c.confidence} confidence) with decision #{other['id']}: {c.reason}",
            file=sys.stderr,
        )


def cmd_conflicts(args) -> None:
    root = Path(args.path).resolve()
    llm_command = shlex.split(args.llm_command) if args.llm_command else None
    found = conflicts_module.detect_conflicts(
        root, file=args.file, symbol=args.symbol,
        llm_arbitrate=args.llm_arbitrate, llm_command=llm_command,
    )
    if not found:
        print("No conflicts detected.")
        return
    for c in found:
        print(f"[{c.rule}, {c.confidence}] #{c.decision_a['id']} vs #{c.decision_b['id']}: {c.reason}")


def cmd_set_status(args) -> None:
    root = Path(args.path).resolve()
    try:
        updated = storage.set_decision_status(root, args.decision_id, args.new_status, note=args.note)
    except ValueError as e:
        print(f"devmemory set-status failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Decision #{updated['id']} status is now {updated['status']}")


def cmd_history(args) -> None:
    root = Path(args.path).resolve()
    try:
        entries = storage.get_decision_history(root, args.decision_id)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not entries:
        print(f"No history recorded for decision #{args.decision_id}.")
        return
    for entry in entries:
        line = f"[{entry['changed_at']}] {entry['event']}"
        if entry.get("old_status") or entry.get("new_status"):
            line += f" ({entry.get('old_status') or '-'} -> {entry.get('new_status') or '-'})"
        if entry.get("changed_by"):
            line += f" by {entry['changed_by']}"
        print(line)
        if entry.get("note"):
            print(f"    {entry['note']}")


def _render_decision_for_compile(record: dict) -> str:
    lines = [_format_decision_line(record), f"    {record['summary']}"]
    if record.get("why"):
        lines.append(f"    why: {record['why']}")
    return "\n".join(lines)


def _print_decisions(root, records: list[dict], command: str, compile_: bool, budget: int) -> None:
    if not compile_:
        storage.log_access(root, "decision", [r["id"] for r in records], command)
        for record in records:
            print(_format_decision_line(record))
            print(f"    {record['summary']}")
        return

    compiled = compiler_module.compile_context(
        records, budget, _render_decision_for_compile, dedup_key=lambda r: (r["summary"], r.get("why")),
    )
    storage.log_access(root, "decision", [r["id"] for r in records], command, estimated_tokens=compiled.estimated_tokens)
    print(compiled.text)
    print(
        f"[compiled: ~{compiled.estimated_tokens} tokens (estimated), "
        f"{compiled.included_items} included, {compiled.omitted_items} omitted]"
    )


def cmd_query(args) -> None:
    root = Path(args.path).resolve()
    if args.text:
        records = storage.search_decisions(root, args.text, all_branches=args.all_branches)
    else:
        records = storage.query_decisions(root, file=args.file, symbol=args.symbol, all_branches=args.all_branches)
    if not records:
        print("No decision records found.")
        return
    _print_decisions(root, records, "query", args.compile, args.budget)


def cmd_log(args) -> None:
    root = Path(args.path).resolve()
    records = storage.log_since(root, args.since, all_branches=args.all_branches)
    if not records:
        print(f"No decisions since {args.since}.")
        return
    _print_decisions(root, records, "log", args.compile, args.budget)


def cmd_roi(args) -> None:
    root = Path(args.path).resolve()
    try:
        report = storage.roi_report(root, since=args.since)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not report:
        print("No decisions recorded yet.")
        return

    labels = {
        "automatic_commit_extraction": "Automatic (git-hook extraction, costs an LLM call per commit)",
        "manual_or_session": "Manual / session-recorded (free)",
    }
    for bucket, counts in sorted(report.items()):
        total, accessed = counts["total"], counts["accessed"]
        pct = (accessed / total * 100) if total else 0
        print(f"{labels.get(bucket, bucket)}: {accessed}/{total} queried again since creation ({pct:.0f}%)")
        if counts["oldest_unaccessed"]:
            print(f"    oldest never-queried record: {counts['oldest_unaccessed']}")
        if counts["token_sample_count"]:
            since_note = f" since {args.since}" if args.since else ""
            print(
                f"    token cost{since_note}: ~{counts['total_estimated_tokens']} estimated token(s) "
                f"across {counts['token_sample_count']} compiled access(es) "
                f"(avg ~{counts['avg_estimated_tokens_per_access']:.0f}/access)"
            )


def cmd_onboard(args) -> None:
    root = Path(args.path).resolve()
    report = onboard_module.onboard(root, dry_run=args.dry_run)
    verb = "Would index" if args.dry_run else "Indexed"
    print(
        f"{verb} {report.indexed} context source(s) ({report.skipped_unchanged} unchanged, "
        f"{report.skipped_unrecognized} unrecognized, {report.failed} failed)."
    )
    if not args.dry_run:
        print(f"Linked {report.linked} context record(s) to graph node(s).")


def cmd_profiles(args) -> None:
    for name in profiles_module.all_names():
        p = profiles_module.get(name)
        print(f"{p.name}: edge_kinds={list(p.edge_kinds)} max_depth={p.max_depth}")
        print(
            f"    decision_links={p.include_decision_links} context_records={p.include_context_records} "
            f"importers={p.include_importers} history={p.include_history}"
        )


def cmd_status(args) -> None:
    root = Path(args.path).resolve()
    print(f"Project root: {root}")

    db_path = storage.get_db_path(root)
    if not db_path.exists():
        print("devmemory store: not initialized (run `devmemory init`)")
        return

    branch = git_utils.current_branch(root)
    print(f"Git branch: {branch or '(not a git repo / detached HEAD -- decisions are project-wide)'}")

    conn = sqlite3.connect(db_path)
    try:
        schema_version = migrations.current_version(conn)
    finally:
        conn.close()
    latest_version = migrations.MIGRATIONS[-1][0]
    pending_note = "" if schema_version == latest_version else " (pending: run `devmemory migrate`)"
    print(f"Schema version: {schema_version}/{latest_version}{pending_note}")

    decisions = storage.query_decisions(root, all_branches=True)
    print(f"Decisions recorded: {len(decisions)}")

    try:
        kstats = knowledge.stats(root)
    except FileNotFoundError:
        kstats = {"nodes": {}, "edges": {}}
    total_nodes = sum(kstats["nodes"].values())
    total_edges = sum(kstats["edges"].values())
    if total_nodes:
        print(f"Knowledge graph: {total_nodes} node(s), {total_edges} edge(s)")
    else:
        print("Knowledge graph: not yet scanned (run `devmemory knowledge scan`)")

    staleness = knowledge.branch_staleness(root)
    if staleness:
        print(staleness)


def cmd_doctor(args) -> None:
    root = Path(args.path).resolve()
    issues = 0

    py_ok = sys.version_info >= (3, 10)
    print(f"[{'ok' if py_ok else 'FAIL'}] Python {sys.version_info.major}.{sys.version_info.minor} (requires >=3.10)")
    issues += 0 if py_ok else 1

    db_path = storage.get_db_path(root)
    initialized = db_path.exists()
    print(
        f"[{'ok' if initialized else 'warn'}] devmemory store "
        f"{'initialized' if initialized else 'not initialized -- run `devmemory init`'}"
    )
    issues += 0 if initialized else 1

    if initialized:
        fts = storage.has_fts5(root)
        print(
            f"[{'ok' if fts else 'warn'}] SQLite FTS5 "
            f"{'available' if fts else 'unavailable -- query --text falls back to a plain LIKE search'}"
        )

    is_git = (root / ".git").exists()
    print(
        f"[{'ok' if is_git else 'warn'}] git repository "
        f"{'detected' if is_git else 'not detected -- decisions stay project-wide; `devmemory install git` is unavailable'}"
    )
    issues += 0 if is_git else 1

    try:
        import mcp  # noqa: F401
        mcp_ok = True
    except ImportError:
        mcp_ok = False
    mcp_note = "installed" if mcp_ok else 'not installed -- pip install "devmemory[mcp]" to use `devmemory mcp-server`'
    print(f"[{'ok' if mcp_ok else 'info'}] MCP server extra {mcp_note}")

    if initialized:
        try:
            kstats = knowledge.stats(root)
        except FileNotFoundError:
            kstats = {"nodes": {}}
        scanned = bool(kstats.get("nodes"))
        print(
            f"[{'ok' if scanned else 'warn'}] knowledge graph "
            f"{'scanned' if scanned else 'never scanned -- run `devmemory knowledge scan`'}"
        )
        issues += 0 if scanned else 1

    if issues:
        print(f"\n{issues} item(s) need attention.")
        sys.exit(1)
    print("\nAll checks passed.")


_EXPLAIN_DISCLAIMER = "(keyword search over recorded decisions/context, not natural-language understanding)"


def _significant_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", (text or "").lower()) if len(w) > 3}


def cmd_memory_query(args) -> None:
    root = Path(args.path).resolve()
    records = storage.search_decisions(root, args.question, all_branches=args.all_branches)
    print(_EXPLAIN_DISCLAIMER)
    if not records:
        print("No matching decisions found.")
        return
    storage.log_access(root, "decision", [r["id"] for r in records], "memory_query")
    compiled = compiler_module.compile_context(
        records, compiler_module.DEFAULT_BUDGET_TOKENS, _render_decision_for_compile,
        dedup_key=lambda r: (r["summary"], r.get("why")),
    )
    print(compiled.text)


def cmd_explain(args) -> None:
    root = Path(args.path).resolve()
    words = _significant_words(args.question)

    decisions = storage.search_decisions(root, args.question, all_branches=args.all_branches)
    context_records = [
        r for r in storage.query_context_records(root) if words & _significant_words(r["content"])
    ]

    print(_EXPLAIN_DISCLAIMER)
    if not decisions and not context_records:
        print("Nothing found for that question.")
        return

    if decisions:
        storage.log_access(root, "decision", [r["id"] for r in decisions], "explain")
        compiled = compiler_module.compile_context(
            decisions, compiler_module.DEFAULT_BUDGET_TOKENS, _render_decision_for_compile,
            dedup_key=lambda r: (r["summary"], r.get("why")),
        )
        print("Decisions:")
        print(compiled.text)

    if context_records:
        print("Project context:")
        for rec in context_records[:10]:
            print(f"  [{rec['kind']}] {rec['source_path']}")


def cmd_context(args) -> None:
    root = Path(args.path).resolve()
    try:
        result = retrieval.traverse(
            root, args.file, args.symbol, profile_name=args.profile,
            max_depth_override=args.max_depth, all_branches=args.all_branches,
        )
    except ValueError as e:
        print(f"devmemory context failed: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    if not result["found"]:
        print(f"No node found for file={args.file!r} symbol={args.symbol!r}")
        return

    if args.compile:
        flat_entries = [e for level in result["levels"] for e in level]

        def _render_level_entry(e: dict) -> str:
            marker = f" ({e['decision_count']} decision(s))" if e["decision_count"] else ""
            return f"{e['file']}::{e['symbol']}{marker}"

        compiled = compiler_module.compile_context(
            flat_entries, args.budget, _render_level_entry, dedup_key=lambda e: (e["file"], e["symbol"]),
        )
        storage.log_access(root, "node", result["node_ids"], "context", estimated_tokens=compiled.estimated_tokens)
        print(f"Profile: {result['profile']}")
        print(compiled.text)
        print(
            f"[compiled: ~{compiled.estimated_tokens} tokens (estimated), "
            f"{compiled.included_items} included, {compiled.omitted_items} omitted]"
        )
        return

    storage.log_access(root, "node", result["node_ids"], "context")

    print(f"Profile: {result['profile']}")
    for depth, entries in enumerate(result["levels"], start=1):
        print(f"Depth {depth}:")
        for e in entries:
            marker = f" ({e['decision_count']} decision(s))" if e["decision_count"] else ""
            print(f"  {e['file']}::{e['symbol']}{marker}")
    if result["importing_files"]:
        print("Importing files (lower confidence):")
        for f in result["importing_files"]:
            print(f"  {f}")
    if result["decision_links"]:
        print(f"Decision links ({len(result['decision_links'])}):")
        for link in result["decision_links"]:
            print(f"  {link['kind']} -> {link['dst_kind']}:{link['dst_ref']}")
    if result["context_records"]:
        print(f"Context records ({len(result['context_records'])}):")
        for rec in result["context_records"]:
            print(f"  [{rec['kind']}] {rec['source_path']}")
    if result["history"]:
        print(f"History ({len(result['history'])}):")
        for h in result["history"]:
            print(f"  superseded at {h['superseded_at']}")


def _import_mcp_server():
    """A separate, trivially-mockable function (rather than inlining the
    import in cmd_mcp_server) so a test can simulate "the 'mcp' extra isn't
    installed" without fighting Python's sys.modules import caching."""
    from . import mcp_server
    return mcp_server


def cmd_mcp_server(args) -> None:
    root = Path(args.path).resolve()
    try:
        mcp_server = _import_mcp_server()
    except ImportError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    mcp_server.main(root)


def cmd_migrate(args) -> None:
    root = Path(args.path).resolve()
    db_path = storage.get_db_path(root)
    if not db_path.exists():
        print(f"devmemory store not initialized at {db_path}. Run `devmemory init` first.", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(db_path)
    try:
        applied = migrations.apply_migrations(conn)
    finally:
        conn.close()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(str(v) for v in applied)}")
    else:
        print("Schema already up to date.")


def cmd_install(args) -> None:
    root = Path(args.path).resolve()
    adapter = PLATFORM_ADAPTERS.get(args.platform)
    if adapter is None:
        print(f"Unknown platform: {args.platform}", file=sys.stderr)
        sys.exit(1)
    paths = adapter.install(root)
    if isinstance(paths, Path):
        paths = [paths]
    for path in paths:
        print(f"Installed devmemory instructions for {args.platform} at {path}")


def cmd_extract(args) -> None:
    root = Path(args.path).resolve()
    llm_command = shlex.split(args.llm_command) if args.llm_command else None
    try:
        records = extract.extract_and_record(root, commit_ref=args.commit, llm_command=llm_command)
    except Exception as e:
        print(f"devmemory extract failed: {e}", file=sys.stderr)
        sys.exit(1)
    if not records:
        print("No decision-worthy changes found in this commit.")
        return
    for record in records:
        print(f"Recorded decision #{record['id']} for {record['file']} (from commit)")


def cmd_check_session(args) -> None:
    root = Path(args.path).resolve()
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    result = session_check.handle_stdin_payload(root, payload)
    if result is not None:
        print(json.dumps(result))


def cmd_knowledge_scan(args) -> None:
    root = Path(args.path).resolve()
    summary = knowledge.scan(root)
    print(
        f"Scanned {summary.files_scanned} file(s), {summary.files_unchanged} unchanged. "
        f"Graph now has {summary.nodes} nodes, {summary.edges} edges."
    )
    staleness = knowledge.branch_staleness(root)
    if staleness:
        print(staleness, file=sys.stderr)


def cmd_knowledge_stats(args) -> None:
    root = Path(args.path).resolve()
    try:
        result = knowledge.stats(root)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not result["nodes"] and not result["edges"]:
        print("Knowledge graph is empty. Run `devmemory knowledge scan` first.")
        return
    print("Nodes:")
    for kind, count in sorted(result["nodes"].items()):
        print(f"  {kind}: {count}")
    print("Edges:")
    for kind, count in sorted(result["edges"].items()):
        print(f"  {kind}: {count}")
    staleness = knowledge.branch_staleness(root)
    if staleness:
        print(staleness, file=sys.stderr)


def cmd_knowledge_render(args) -> None:
    root = Path(args.path).resolve()
    try:
        diagram = knowledge.render_mermaid(root, detail=args.detail, file=args.file)
    except (ValueError, FileNotFoundError) as e:
        print(f"devmemory knowledge render failed: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Wrote {knowledge.get_mermaid_path(root)}")
    print(diagram)


def cmd_knowledge_inspect(args) -> None:
    root = Path(args.path).resolve()
    try:
        result = knowledge.inspect(root, file=args.file, symbol=args.symbol)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not result["found"]:
        print(f"No node found for file={args.file!r} symbol={args.symbol!r}")
        return
    storage.log_access(root, "node", [result["node"]["id"]], "inspect")

    node = result["node"]
    target = node["file"] + (f"::{node['symbol']}" if node["symbol"] else "")
    print(f"{node['kind']} {target}")
    if node.get("language"):
        print(f"  language: {node['language']}")
    if node.get("line_start"):
        print(f"  lines: {node['line_start']}-{node['line_end']}")
    if node.get("signature"):
        print(f"  signature: {node['signature']}")
    if node.get("docstring"):
        print(f"  docstring: {node['docstring']}")

    if result["outgoing"]:
        print("  outgoing:")
        for e in result["outgoing"]:
            dst = e["file"] + (f"::{e['symbol']}" if e["symbol"] else "")
            print(f"    {e['edge_kind']} -> {dst}")
    if result["incoming"]:
        print("  incoming:")
        for e in result["incoming"]:
            src = e["file"] + (f"::{e['symbol']}" if e["symbol"] else "")
            print(f"    {src} -> {e['edge_kind']}")

    if result["decisions"]:
        print(f"  devmemory decisions ({len(result['decisions'])}):")
        for d in result["decisions"]:
            print(f"    #{d['id']} [{d['created_at']}] {d['summary']}")
    else:
        print("  devmemory decisions: none recorded yet")


def cmd_knowledge_impact(args) -> None:
    root = Path(args.path).resolve()
    try:
        result = knowledge.impact(root, file=args.file, symbol=args.symbol, max_depth=args.max_depth)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    if not result["found"]:
        print(f"No node found for file={args.file!r} symbol={args.symbol!r}")
        return
    storage.log_access(root, "node", result["node_ids"], "impact")

    if not result["levels"] and not result["importing_files"]:
        print("No other code depends on this symbol (as far as the graph can tell).")
        return

    for depth, entries in enumerate(result["levels"], start=1):
        print(f"Depth {depth}:")
        for e in entries:
            marker = f" ({e['decision_count']} decision(s))" if e["decision_count"] else ""
            print(f"  {e['file']}::{e['symbol']}{marker}")

    if result["importing_files"]:
        print("Files that import this symbol's file (may or may not use it directly):")
        for f in result["importing_files"]:
            print(f"  {f}")


def cmd_knowledge_history(args) -> None:
    root = Path(args.path).resolve()
    entries = knowledge.node_history(root, file=args.file, symbol=args.symbol)
    if not entries:
        print("No history recorded for this symbol (unchanged since it was first seen, or never seen).")
        return
    for e in entries:
        print(f"[{e['superseded_at']}] {e['kind']} {e['file']}::{e['symbol']}" if e['symbol'] else f"[{e['superseded_at']}] {e['kind']} {e['file']}")
        if e.get("signature"):
            print(f"    signature: {e['signature']}")
        if e.get("docstring"):
            print(f"    docstring: {e['docstring']}")


def cmd_knowledge_renames(args) -> None:
    root = Path(args.path).resolve()
    if args.mark_resolved is not None:
        knowledge.mark_rename_candidate_resolved(root, args.mark_resolved)
        print(f"Marked rename candidate #{args.mark_resolved} as resolved.")
        return

    resolved = args.resolved if args.resolved else None
    candidates = knowledge.rename_candidates(root, resolved=resolved)
    if not candidates:
        print("No rename candidates.")
        return
    for c in candidates:
        status = "resolved" if c["resolved"] else "open"
        print(
            f"#{c['id']} [{status}, {c['confidence']} confidence] "
            f"{c['old_node_file']}::{c['old_node_symbol']} -> node #{c['new_node_id']} "
            f"(detected {c['detected_at']})"
        )


def get_version() -> str:
    """Single source of truth is pyproject.toml's `version` -- read back via
    package metadata so this is correct whether devmemory is an editable
    install, a built wheel, or installed with `uv tool install --from
    git+...` -- never a second, hardcoded copy of the version string that
    could drift from pyproject.toml.
    """
    try:
        return importlib.metadata.version("devmemory")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed as a package)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devmemory")
    parser.add_argument("--version", action="version", version=f"devmemory {get_version()}")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init", help="Initialize the devmemory store")
    p_init.set_defaults(func=cmd_init)

    p_record = subparsers.add_parser("record", help="Record a decision")
    p_record.add_argument("--file", required=True, help="File the decision applies to")
    p_record.add_argument("--symbol", help="Function/class/symbol the decision applies to")
    p_record.add_argument("--summary", required=True, help="What changed")
    p_record.add_argument("--why", help="Why it changed")
    p_record.add_argument("--rejected", help="What alternative was rejected, and why")
    p_record.add_argument("--tags", help="Comma-separated tags")
    p_record.add_argument("--session", help="Session/conversation reference")
    p_record.add_argument("--author", help="Who made the decision")
    p_record.add_argument(
        "--status", choices=sorted(storage.VALID_STATUSES), default="accepted", help="Decision status (default: accepted)"
    )
    p_record.add_argument("--alternatives", help="Alternatives considered, distinct from --rejected's prose")
    p_record.add_argument("--consequences", help="Trade-offs/consequences of this decision")
    p_record.add_argument("--scope", help="Path/glob prefix this decision applies to, beyond just --file/--symbol")
    p_record.add_argument("--owner", help="Who owns this decision")
    p_record.add_argument(
        "--supersedes", type=int, metavar="ID",
        help="Mark decision #ID as superseded by this new one",
    )
    p_record.add_argument(
        "--global", action="store_true", dest="global_",
        help="Record project-wide instead of scoped to the current git branch",
    )
    p_record.set_defaults(func=cmd_record)

    p_set_status = subparsers.add_parser("set-status", help="Change a decision's status")
    p_set_status.add_argument("decision_id", type=int)
    p_set_status.add_argument("new_status", choices=sorted(storage.VALID_STATUSES))
    p_set_status.add_argument("--note", help="Why the status changed")
    p_set_status.set_defaults(func=cmd_set_status)

    p_history = subparsers.add_parser("history", help="Status/supersession history for one decision")
    p_history.add_argument("--decision-id", type=int, required=True, dest="decision_id")
    p_history.set_defaults(func=cmd_history)

    p_conflicts = subparsers.add_parser(
        "conflicts", help="Detect possibly-conflicting active decisions for a file/symbol"
    )
    p_conflicts.add_argument("--file", required=True)
    p_conflicts.add_argument("--symbol")
    p_conflicts.add_argument(
        "--llm-arbitrate", action="store_true",
        help="Use an LLM to confirm/reject keyword-negation candidates (opt-in; costs an LLM call)",
    )
    p_conflicts.add_argument(
        "--llm-command", help="Override the LLM command line (shell-split); for tests or alternate providers"
    )
    p_conflicts.set_defaults(func=cmd_conflicts)

    p_profiles = subparsers.add_parser("profiles", help="List available traversal profiles and their configs")
    p_profiles.set_defaults(func=cmd_profiles)

    p_context = subparsers.add_parser(
        "context", help="Task-specific graph traversal around a symbol, using a named profile"
    )
    p_context.add_argument("--file", required=True)
    p_context.add_argument("--symbol", required=True)
    p_context.add_argument(
        "--profile", default="explanation", choices=profiles_module.all_names(),
        help="Traversal profile (default: explanation)",
    )
    p_context.add_argument("--max-depth", type=int, help="Override the profile's default max_depth")
    p_context.add_argument("--all-branches", action="store_true", help="Include decisions recorded on other branches too")
    p_context.add_argument(
        "--compile", action="store_true", help="Compile the traversal levels into a deduplicated, token-budgeted block"
    )
    p_context.add_argument(
        "--budget", type=int, default=compiler_module.DEFAULT_BUDGET_TOKENS,
        help=f"Token budget for --compile (estimated; default: {compiler_module.DEFAULT_BUDGET_TOKENS})",
    )
    p_context.set_defaults(func=cmd_context)

    p_status = subparsers.add_parser("status", help="Repository identity, storage, schema, and graph state at a glance")
    p_status.set_defaults(func=cmd_status)

    p_doctor = subparsers.add_parser("doctor", help="Environment/installation health check")
    p_doctor.set_defaults(func=cmd_doctor)

    p_explain = subparsers.add_parser(
        "explain", help=f"Free-text question over decisions/context. {_EXPLAIN_DISCLAIMER}"
    )
    p_explain.add_argument("question")
    p_explain.add_argument("--all-branches", action="store_true", help="Include decisions recorded on other branches too")
    p_explain.set_defaults(func=cmd_explain)

    p_memory = subparsers.add_parser("memory", help="Query recorded decisions by free text")
    memory_subparsers = p_memory.add_subparsers(dest="memory_command", required=True)
    p_memory_query = memory_subparsers.add_parser(
        "query", help=f"Free-text question over recorded decisions. {_EXPLAIN_DISCLAIMER}"
    )
    p_memory_query.add_argument("question")
    p_memory_query.add_argument("--all-branches", action="store_true", help="Include decisions recorded on other branches too")
    p_memory_query.set_defaults(func=cmd_memory_query)

    p_query = subparsers.add_parser("query", help="Query decisions scoped to a file/symbol, or full-text")
    p_query.add_argument("--file", help="Exact file path to filter by")
    p_query.add_argument("--symbol", help="Substring match on symbol name")
    p_query.add_argument(
        "--text", help="Full-text search across summary/why/rejected/tags (standalone; ignores --file/--symbol)"
    )
    p_query.add_argument(
        "--all-branches", action="store_true", help="Include decisions recorded on other branches too"
    )
    p_query.add_argument(
        "--compile", action="store_true", help="Compile output into a deduplicated, token-budgeted block"
    )
    p_query.add_argument(
        "--budget", type=int, default=compiler_module.DEFAULT_BUDGET_TOKENS,
        help=f"Token budget for --compile (estimated; default: {compiler_module.DEFAULT_BUDGET_TOKENS})",
    )
    p_query.set_defaults(func=cmd_query)

    p_log = subparsers.add_parser("log", help="List decisions since a date, free of LLM cost")
    p_log.add_argument("--since", required=True, help="ISO date/datetime, e.g. 2026-10-01")
    p_log.add_argument(
        "--all-branches", action="store_true", help="Include decisions recorded on other branches too"
    )
    p_log.add_argument(
        "--compile", action="store_true", help="Compile output into a deduplicated, token-budgeted block"
    )
    p_log.add_argument(
        "--budget", type=int, default=compiler_module.DEFAULT_BUDGET_TOKENS,
        help=f"Token budget for --compile (estimated; default: {compiler_module.DEFAULT_BUDGET_TOKENS})",
    )
    p_log.set_defaults(func=cmd_log)

    p_roi = subparsers.add_parser(
        "roi", help="Hit rate of automatic vs. manual decision records -- are they ever queried again?"
    )
    p_roi.add_argument(
        "--since", help="Scope the token-cost section to accesses at/after this ISO date (independent of bucketing)"
    )
    p_roi.set_defaults(func=cmd_roi)

    p_migrate = subparsers.add_parser(
        "migrate", help="Apply any pending schema migrations to an existing store"
    )
    p_migrate.set_defaults(func=cmd_migrate)

    p_onboard = subparsers.add_parser(
        "onboard", help="Discover and index existing AI-instruction/architectural context in this repo"
    )
    p_onboard.add_argument("--dry-run", action="store_true", help="Report what would be indexed without writing anything")
    p_onboard.set_defaults(func=cmd_onboard)

    p_mcp = subparsers.add_parser(
        "mcp-server", help="Run the devmemory MCP server (stdio transport; requires the 'mcp' extra)"
    )
    p_mcp.set_defaults(func=cmd_mcp_server)

    p_install = subparsers.add_parser("install", help="Install tool-specific instructions")
    p_install.add_argument("platform", choices=sorted(PLATFORM_ADAPTERS))
    p_install.set_defaults(func=cmd_install)

    p_extract = subparsers.add_parser(
        "extract", help="Synthesize a decision record from a commit via an LLM call"
    )
    p_extract.add_argument("--commit", default="HEAD", help="Commit ref to extract from (default: HEAD)")
    p_extract.add_argument(
        "--llm-command",
        help="Override the LLM command line (shell-split); for tests or alternate providers",
    )
    p_extract.set_defaults(func=cmd_extract)

    p_check = subparsers.add_parser(
        "check-session", help="Claude Code Stop-hook: nudge for a missing devmemory record"
    )
    p_check.set_defaults(func=cmd_check_session)

    p_scan = subparsers.add_parser(
        "scan", help="Alias for `devmemory knowledge scan` -- index the repo (incremental)"
    )
    p_scan.set_defaults(func=cmd_knowledge_scan)

    p_knowledge = subparsers.add_parser("knowledge", help="Code-structure knowledge graph")
    knowledge_subparsers = p_knowledge.add_subparsers(dest="knowledge_command", required=True)

    p_know_scan = knowledge_subparsers.add_parser(
        "scan", help="Scan the repo (Python/JS/TS) and update the knowledge graph"
    )
    p_know_scan.set_defaults(func=cmd_knowledge_scan)

    p_know_stats = knowledge_subparsers.add_parser("stats", help="Node/edge counts by kind")
    p_know_stats.set_defaults(func=cmd_knowledge_stats)

    p_know_render = knowledge_subparsers.add_parser(
        "render", help="Render the graph as Mermaid to .devmemory/knowledge/graph.mmd"
    )
    p_know_render.add_argument(
        "--detail", choices=["files", "symbols"], default="files",
        help="'files' (default): whole-repo import graph. 'symbols': one file's functions/classes/calls",
    )
    p_know_render.add_argument("--file", help="Required with --detail symbols")
    p_know_render.set_defaults(func=cmd_knowledge_render)

    p_know_inspect = knowledge_subparsers.add_parser(
        "inspect", help="Full metadata, graph edges, and devmemory decisions for one node"
    )
    p_know_inspect.add_argument("--file", required=True)
    p_know_inspect.add_argument("--symbol", help="Omit to inspect the file node itself")
    p_know_inspect.set_defaults(func=cmd_knowledge_inspect)

    p_know_impact = knowledge_subparsers.add_parser(
        "impact", help="Transitive callers/referencers of a symbol -- what breaks if you change it"
    )
    p_know_impact.add_argument("--file", required=True)
    p_know_impact.add_argument("--symbol", required=True)
    p_know_impact.add_argument("--max-depth", type=int, default=3, help="How many caller levels to traverse (default: 3)")
    p_know_impact.set_defaults(func=cmd_knowledge_impact)

    p_know_history = knowledge_subparsers.add_parser(
        "history", help="Prior snapshots of a symbol, recorded only when its body actually changed"
    )
    p_know_history.add_argument("--file", required=True)
    p_know_history.add_argument("--symbol", help="Omit for the file node itself")
    p_know_history.set_defaults(func=cmd_knowledge_history)

    p_know_renames = knowledge_subparsers.add_parser(
        "renames", help="Advisory same-body-hash matches between disappeared and newly-appeared symbols"
    )
    p_know_renames.add_argument("--resolved", action="store_true", help="Show only candidates already marked resolved")
    p_know_renames.add_argument("--mark-resolved", type=int, metavar="ID", help="Mark candidate #ID as resolved")
    p_know_renames.set_defaults(func=cmd_knowledge_renames)

    # `--path` is deliberately re-declared (with an independent Action object,
    # not shared via `parents=`) on every subparser above, so it can appear
    # either before or after the subcommand. Each subparser's copy defaults to
    # SUPPRESS so that when --path isn't given after the subcommand, nothing
    # is injected into the sub-namespace and the top-level value (parsed
    # before the subcommand, or its own default) survives untouched. A shared
    # Action instance would NOT work here: argparse's `set_defaults()` mutates
    # a matching action's `.default` in place, so sharing the same `--path`
    # action between parsers (e.g. via `parents=`) means fixing one parser's
    # default silently changes every other parser holding that same instance.
    for subparser in subparsers.choices.values():
        subparser.add_argument("--path", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # `knowledge <subcommand>` is a subparser nested inside a subparser, so it
    # needs the same retrofit one level deeper for `--path` to work after
    # either the outer or inner subcommand (e.g. `knowledge scan --path X`).
    for subparser in knowledge_subparsers.choices.values():
        subparser.add_argument("--path", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    for subparser in memory_subparsers.choices.values():
        subparser.add_argument("--path", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
