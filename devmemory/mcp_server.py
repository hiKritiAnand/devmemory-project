"""MCP server exposing devmemory's core engine as six tools, each a thin
adapter over functions already used by the CLI (storage/knowledge/retrieval/
conflicts) -- no graph or retrieval logic is reimplemented here, and every
tool's traversal goes through the same retrieval.traverse() the CLI's
`context`/`knowledge impact` commands use.

Optional: requires the `mcp` extra (`pip install devmemory[mcp]`). The base
CLI never imports this module eagerly -- `devmemory mcp-server` imports it
lazily and prints an install hint if the dependency is missing.

Transport: stdio, the standard for a locally-launched, per-project MCP
server (how Claude Code/Cursor both launch one).
"""

from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
except ImportError as e:
    raise ImportError(
        "The devmemory MCP server requires the optional 'mcp' package. "
        "Install it with: pip install devmemory[mcp]"
    ) from e

from . import compiler, conflicts, knowledge, retrieval, storage

DEFAULT_BUDGET = compiler.DEFAULT_BUDGET_TOKENS


def _render_decision(r: dict) -> str:
    target = r["file"] + (f"::{r['symbol']}" if r.get("symbol") else "")
    lines = [f"#{r['id']} [{r.get('status', 'accepted')}] {target}", r["summary"]]
    if r.get("why"):
        lines.append(f"why: {r['why']}")
    return "\n".join(lines)


def _compile_decisions(root, records: list[dict], command: str) -> dict:
    compiled = compiler.compile_context(
        records, DEFAULT_BUDGET, _render_decision, dedup_key=lambda r: (r["summary"], r.get("why")),
    )
    if records:
        storage.log_access(root, "decision", [r["id"] for r in records], command, estimated_tokens=compiled.estimated_tokens)
    return {
        "text": compiled.text, "estimated_tokens": compiled.estimated_tokens,
        "is_estimate": compiled.is_estimate, "included": compiled.included_items, "omitted": compiled.omitted_items,
    }


def _render_level_entry(e: dict) -> str:
    marker = f" ({e['decision_count']} decision(s))" if e["decision_count"] else ""
    return f"{e['file']}::{e['symbol']}{marker}"


def _compile_traverse_result(root, result: dict, command: str) -> dict:
    if not result["found"]:
        return {"found": False, "file": result["file"], "symbol": result["symbol"]}

    flat_entries = [e for level in result["levels"] for e in level]
    compiled = compiler.compile_context(
        flat_entries, DEFAULT_BUDGET, _render_level_entry, dedup_key=lambda e: (e["file"], e["symbol"]),
    )
    storage.log_access(root, "node", result["node_ids"], command, estimated_tokens=compiled.estimated_tokens)
    return {
        "found": True, "profile": result.get("profile"),
        "text": compiled.text, "estimated_tokens": compiled.estimated_tokens,
        "is_estimate": compiled.is_estimate, "included": compiled.included_items, "omitted": compiled.omitted_items,
        "importing_files": result.get("importing_files", []),
    }


def build_server(root) -> "MCPServer":
    root = Path(root)
    server = MCPServer(
        name="devmemory",
        description="Scoped engineering memory for this repository: decisions, code structure, and development history.",
    )

    @server.tool(description="Search recorded decisions by free text, and/or an exact file and substring symbol.")
    def devmemory_search(text: str | None = None, file: str | None = None, symbol: str | None = None) -> dict:
        records = storage.search_decisions(root, text) if text else storage.query_decisions(root, file=file, symbol=symbol)
        return _compile_decisions(root, records, "mcp_search")

    @server.tool(description="Explain a symbol: its structure plus related decisions and context ('explanation' profile).")
    def devmemory_explain(file: str, symbol: str | None = None) -> dict:
        if symbol is None:
            result = knowledge.inspect(root, file)
            if not result["found"]:
                return {"found": False, "file": file, "symbol": symbol}
            storage.log_access(root, "node", [result["node"]["id"]], "mcp_explain")
            return {"found": True, "node": result["node"], "decisions": result["decisions"]}
        result = retrieval.traverse(root, file, symbol, profile_name="explanation")
        return _compile_traverse_result(root, result, "mcp_explain")

    @server.tool(description="Transitive callers/referencers of a symbol -- what breaks if you change it.")
    def devmemory_impact(file: str, symbol: str, max_depth: int | None = None) -> dict:
        result = retrieval.traverse(root, file, symbol, profile_name="change_impact", max_depth_override=max_depth)
        return _compile_traverse_result(root, result, "mcp_impact")

    @server.tool(description="Task-specific graph traversal around a symbol, using a named traversal profile.")
    def devmemory_context(file: str, symbol: str, profile: str = "explanation", max_depth: int | None = None) -> dict:
        result = retrieval.traverse(root, file, symbol, profile_name=profile, max_depth_override=max_depth)
        return _compile_traverse_result(root, result, "mcp_context")

    @server.tool(description="Status/supersession history for a decision, or prior snapshots of a code symbol.")
    def devmemory_history(file: str | None = None, symbol: str | None = None, decision_id: int | None = None) -> dict:
        if decision_id is not None:
            entries = storage.get_decision_history(root, decision_id)
            return {"decision_history": entries}
        if file is not None:
            return {"node_history": knowledge.node_history(root, file, symbol)}
        return {"decision_history": [], "node_history": []}

    @server.tool(description="Record a new decision, surfacing any detected conflicts with existing active decisions.")
    def devmemory_record(
        file: str, summary: str, symbol: str | None = None, why: str | None = None,
        rejected: str | None = None, status: str = "accepted",
    ) -> dict:
        record = storage.add_decision(root, file=file, summary=summary, symbol=symbol, why=why, rejected=rejected, status=status)
        found_conflicts = conflicts.detect_conflicts(root, file, symbol)
        own_conflicts = [
            c for c in found_conflicts if record["id"] in (c.decision_a["id"], c.decision_b["id"])
        ]
        return {
            "recorded": record,
            "conflicts": [
                {"rule": c.rule, "confidence": c.confidence, "reason": c.reason,
                 "other_decision_id": c.decision_b["id"] if c.decision_a["id"] == record["id"] else c.decision_a["id"]}
                for c in own_conflicts
            ],
        }

    return server


def main(root=".") -> None:
    server = build_server(root)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
