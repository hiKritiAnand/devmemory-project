"""Shared retrieval/traversal engine behind every CLI/MCP entry point that
walks the graph for a task -- one BFS implementation, driven by a named
TraversalProfile's config, not a new algorithm per task.

knowledge.impact() is a thin wrapper over traverse(profile_name=
"change_impact"), preserving its exact pre-existing output contract
(found/file/symbol/levels/importing_files/node_ids) -- the "change_impact"
profile's config was chosen to exactly match what impact() already did
before this module existed.
"""

from pathlib import Path

from . import profiles as profiles_module
from . import storage


def _applicable_scopes(file: str) -> list[str | None]:
    """Every directory prefix of `file`, root-to-leaf, plus None
    (project-wide) -- matches onboard.py's scope_path convention."""
    parts = Path(file).parent.parts
    scopes: list[str | None] = [None]
    for i in range(len(parts)):
        scopes.append("/".join(parts[: i + 1]))
    return scopes


def traverse(
    root, file: str, symbol: str, profile_name: str,
    max_depth_override: int | None = None, all_branches: bool = False,
) -> dict:
    profile = profiles_module.get(profile_name)
    max_depth = max_depth_override if max_depth_override is not None else profile.max_depth
    edge_kinds = profile.edge_kinds or ("calls", "references")

    root = Path(root)
    conn = storage._connect(root)
    try:
        start = conn.execute(
            "SELECT id FROM nodes WHERE file = ? AND symbol = ?", (file, symbol)
        ).fetchone()
        if start is None:
            return {"found": False, "file": file, "symbol": symbol, "profile": profile.name}

        visited = {start["id"]}
        frontier = [start["id"]]
        levels: list[list[dict]] = []

        for _ in range(max_depth):
            if not frontier:
                break
            node_placeholders = ",".join("?" * len(frontier))
            kind_placeholders = ",".join("?" * len(edge_kinds))
            rows = conn.execute(
                f"""
                SELECT DISTINCT sn.id, sn.file, sn.symbol, sn.kind
                FROM edges e JOIN nodes sn ON sn.id = e.src_node_id
                WHERE e.dst_node_id IN ({node_placeholders}) AND e.kind IN ({kind_placeholders})
                """,
                [*frontier, *edge_kinds],
            ).fetchall()
            new_nodes = [r for r in rows if r["id"] not in visited]
            if not new_nodes:
                break
            visited.update(r["id"] for r in new_nodes)

            level_entries = []
            for n in new_nodes:
                decisions = storage.query_decisions(root, file=n["file"], symbol=n["symbol"], all_branches=all_branches)
                level_entries.append({
                    "file": n["file"], "symbol": n["symbol"], "kind": n["kind"],
                    "decision_count": len(decisions),
                })
            levels.append(level_entries)
            frontier = [r["id"] for r in new_nodes]

        importing_files: list[str] = []
        if profile.include_importers:
            importing_files = [
                r["file"] for r in conn.execute(
                    """
                    SELECT DISTINCT sn.file FROM edges e
                    JOIN nodes sn ON sn.id = e.src_node_id
                    JOIN nodes dn ON dn.id = e.dst_node_id
                    WHERE e.kind = 'imports' AND dn.file = ? AND dn.symbol IS NULL AND sn.file != ?
                    """,
                    (file, file),
                ).fetchall()
            ]

        decision_links: list[dict] = []
        if profile.include_decision_links and visited:
            placeholders = ",".join("?" * len(visited))
            decision_links = [
                dict(r) for r in conn.execute(
                    f"SELECT * FROM decision_links WHERE src_kind = 'node' AND src_ref IN ({placeholders})",
                    [str(i) for i in visited],
                ).fetchall()
            ]

        context_records: list[dict] = []
        if profile.include_context_records:
            by_id: dict[int, dict] = {}
            non_null_scopes = [s for s in _applicable_scopes(file) if s is not None]
            sql = "SELECT * FROM context_records WHERE scope_path IS NULL"
            params: list = []
            if non_null_scopes:
                sql += f" OR scope_path IN ({','.join('?' * len(non_null_scopes))})"
                params.extend(non_null_scopes)
            for row in conn.execute(sql, params).fetchall():
                by_id[row["id"]] = dict(row)

            doc_ids = {
                int(link["dst_ref"]) for link in decision_links
                if link["kind"] == "DOCUMENTED_BY" and link["dst_kind"] == "context_record"
            }
            if doc_ids:
                placeholders = ",".join("?" * len(doc_ids))
                for row in conn.execute(
                    f"SELECT * FROM context_records WHERE id IN ({placeholders})", list(doc_ids)
                ).fetchall():
                    by_id[row["id"]] = dict(row)
            context_records = list(by_id.values())

        history: list[dict] = []
        if profile.include_history:
            history = [
                dict(r) for r in conn.execute(
                    "SELECT * FROM node_history WHERE file = ? AND symbol = ? ORDER BY superseded_at DESC LIMIT 10",
                    (file, symbol),
                ).fetchall()
            ]

        return {
            "found": True,
            "file": file,
            "symbol": symbol,
            "profile": profile.name,
            "levels": levels,
            "importing_files": importing_files,
            "decision_links": decision_links,
            "context_records": context_records,
            "history": history,
            "node_ids": sorted(visited - {start["id"]}),
        }
    finally:
        conn.close()
