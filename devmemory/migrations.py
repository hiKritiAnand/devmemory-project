"""Schema migration framework.

`init_store()`'s original approach was `CREATE TABLE IF NOT EXISTS` only --
it silently no-ops on an already-initialized `.devmemory/memory.db` the
moment a column or table is added to `storage.SCHEMA`, instead of altering
anything. From here on, every schema change beyond the Phase-1 baseline is an
entry in MIGRATIONS, applied in order, each in its own transaction, tracked
in `schema_migrations` so it runs exactly once per database -- including a
database created before this module existed (it starts at version 0 and
catches up). SQLite has no `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so any
migration that adds a column must guard itself with `_has_column`/
`add_column_if_missing`.

FTS5 virtual tables can't be `ALTER`'d, so decisions_fts's column set is
deliberately frozen (summary/why/rejected/tags) -- new decision fields stay
out of the FTS index rather than forcing a drop+recreate+repopulate
migration.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Callable

SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """`ddl` is the column definition only, e.g. 'status TEXT NOT NULL DEFAULT \\'accepted\\''."""
    if not has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migration_0001_baseline(conn: sqlite3.Connection) -> None:
    """No-op: storage.SCHEMA/FTS_SCHEMA already create the baseline tables via
    CREATE TABLE IF NOT EXISTS, run just before migrations apply. This entry
    exists purely to anchor version numbering at 1 for every database,
    including ones that predate this module."""


def _migration_0002_decision_status_and_provenance(conn: sqlite3.Connection) -> None:
    """Decision status/supersession/provenance (status, alternatives,
    consequences, scope, owner, superseded_by, origin, confidence,
    verification_status, updated_at) plus a parallel provenance pair on
    edges (origin, confidence, verification_status), plus an append-only
    `decision_history` audit trail. Deliberately NOT added to decisions_fts
    (frozen at summary/why/rejected/tags -- FTS5 virtual tables can't be
    ALTER'd, see module docstring).
    """
    add_column_if_missing(conn, "decisions", "status", "status TEXT NOT NULL DEFAULT 'accepted'")
    add_column_if_missing(conn, "decisions", "alternatives", "alternatives TEXT")
    add_column_if_missing(conn, "decisions", "consequences", "consequences TEXT")
    add_column_if_missing(conn, "decisions", "scope", "scope TEXT")
    add_column_if_missing(conn, "decisions", "owner", "owner TEXT")
    add_column_if_missing(conn, "decisions", "superseded_by", "superseded_by INTEGER REFERENCES decisions(id)")
    add_column_if_missing(conn, "decisions", "origin", "origin TEXT NOT NULL DEFAULT 'developer-input'")
    add_column_if_missing(conn, "decisions", "confidence", "confidence REAL NOT NULL DEFAULT 1.0")
    add_column_if_missing(conn, "decisions", "verification_status", "verification_status TEXT NOT NULL DEFAULT 'current'")
    add_column_if_missing(conn, "decisions", "updated_at", "updated_at TEXT")

    add_column_if_missing(conn, "edges", "origin", "origin TEXT NOT NULL DEFAULT 'source-parse'")
    # No DEFAULT 1.0 here, unlike decisions.confidence above: existing
    # parse-based edges have no real precision to assign, so NULL ("no
    # estimate made") is more honest than a fabricated 1.0.
    add_column_if_missing(conn, "edges", "confidence", "confidence REAL")
    add_column_if_missing(conn, "edges", "verification_status", "verification_status TEXT NOT NULL DEFAULT 'current'")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL REFERENCES decisions(id),
            event TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            note TEXT,
            changed_by TEXT,
            changed_at TEXT NOT NULL
        );
        """
    )

    # The DEFAULT above already backfilled every pre-existing row's `origin`
    # to 'developer-input'; rows actually created by the git-hook LLM
    # extraction path are distinguishable via the existing `session_ref`
    # convention and should be attributed to 'git' instead.
    conn.execute("UPDATE decisions SET origin = 'git' WHERE session_ref LIKE 'commit:%'")


def _migration_0003_decision_links(conn: sqlite3.Connection) -> None:
    """Polymorphic relationship table for every relationship that isn't
    node<->node (those stay in `edges`, see relationships.py): decision<->
    decision (SUPERSEDES), decision<->node (DOCUMENTED_BY), decision<->commit
    (INTRODUCED_BY). Mirrors the existing access_log.ref_kind/ref_id
    convention rather than inventing a new one.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            src_kind TEXT NOT NULL,
            src_ref TEXT NOT NULL,
            dst_kind TEXT NOT NULL,
            dst_ref TEXT NOT NULL,
            kind TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'model-inference',
            confidence REAL,
            verification_status TEXT NOT NULL DEFAULT 'current',
            created_at TEXT NOT NULL
        );
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_links_src ON decision_links(src_kind, src_ref)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_decision_links_dst ON decision_links(dst_kind, dst_ref)")


def _migration_0004_node_versioning(conn: sqlite3.Connection) -> None:
    """Lighter-weight node versioning (Phase 6): `nodes` gets a `body_hash`
    (the symbol's own source span, distinct from `content_hash` which is the
    whole file's), plus first/last-seen timestamps. `node_history` is an
    append-only snapshot of a symbol row taken right before a rescan
    replaces it -- only when its body actually changed, never on every scan.
    `node_rename_candidates` records a same-body-hash match between a symbol
    that disappeared and one that newly appeared, advisory only, never
    auto-merged into a single identity.
    """
    add_column_if_missing(conn, "nodes", "body_hash", "body_hash TEXT")
    add_column_if_missing(conn, "nodes", "first_seen_at", "first_seen_at TEXT")
    add_column_if_missing(conn, "nodes", "last_seen_at", "last_seen_at TEXT")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER NOT NULL REFERENCES nodes(id),
            kind TEXT,
            file TEXT,
            symbol TEXT,
            language TEXT,
            content_hash TEXT,
            body_hash TEXT,
            line_start INTEGER,
            line_end INTEGER,
            signature TEXT,
            docstring TEXT,
            superseded_at TEXT NOT NULL,
            scan_id TEXT
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_rename_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_node_file TEXT NOT NULL,
            old_node_symbol TEXT,
            new_node_id INTEGER NOT NULL REFERENCES nodes(id),
            basis TEXT NOT NULL,
            confidence TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            resolved INTEGER NOT NULL DEFAULT 0
        );
        """
    )


def _migration_0005_branch_isolation(conn: sqlite3.Connection) -> None:
    """`decisions.branch` (NULL = project-wide/unscoped -- every pre-existing
    row stays globally visible, never silently hidden) scopes decisions/
    decision_history/decision_links/context_records to the branch they were
    recorded on, since authored rationale is what risks leaking across
    branches. `nodes.scanned_branch` is advisory only, not a partition key --
    the structural graph already reflects whatever's on disk at scan time,
    so it's never branch-filtered on read, only used for a staleness
    warning (see knowledge.branch_staleness()).
    """
    add_column_if_missing(conn, "decisions", "branch", "branch TEXT")
    add_column_if_missing(conn, "nodes", "scanned_branch", "scanned_branch TEXT")


def _migration_0006_context_records(conn: sqlite3.Connection) -> None:
    """Retrospective-onboarding context sources (README/AGENTS.md/CLAUDE.md/
    cursor rules/copilot instructions/ADRs/OpenAPI specs/migration dirs) --
    one row per source file, upserted by `source_path` (the unique index), so
    re-onboarding an unchanged repo updates nothing. Linked to graph nodes
    via `decision_links` (kind='DOCUMENTED_BY'), not a column here, to reuse
    the existing polymorphic table rather than a parallel one.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS context_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            source_path TEXT NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            scope_path TEXT,
            origin TEXT NOT NULL DEFAULT 'onboarding',
            confidence REAL,
            verification_status TEXT NOT NULL DEFAULT 'current',
            branch TEXT,
            content_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_records_source ON context_records(source_path)"
    )


def _migration_0007_access_log_token_estimates(conn: sqlite3.Connection) -> None:
    """Lets `roi_report` (Phase 12) report real token-cost evidence per
    access, not just hit-rate -- populated only by callers that actually ran
    something through devmemory/compiler.py (CLI `--compile` or an MCP tool
    call); a plain uncompiled CLI read logs NULL here, honestly, rather than
    a fabricated zero.
    """
    add_column_if_missing(conn, "access_log", "estimated_tokens", "estimated_tokens INTEGER")


# (version, name, fn(conn)) in strictly ascending version order. Append new
# entries here; never edit or reorder an already-shipped one.
MIGRATIONS: list[tuple[int, str, Callable[[sqlite3.Connection], None]]] = [
    (1, "baseline_schema", _migration_0001_baseline),
    (2, "decision_status_and_provenance", _migration_0002_decision_status_and_provenance),
    (3, "decision_links", _migration_0003_decision_links),
    (4, "node_versioning", _migration_0004_node_versioning),
    (5, "branch_isolation", _migration_0005_branch_isolation),
    (6, "context_records", _migration_0006_context_records),
    (7, "access_log_token_estimates", _migration_0007_access_log_token_estimates),
]


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(SCHEMA_MIGRATIONS_TABLE)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply any pending migrations in order, each inside its own
    transaction. Returns the newly-applied version numbers (empty if the
    database is already current). A migration that raises leaves
    `schema_migrations` and all data exactly as they were before it started
    -- never partially applied -- and the exception propagates.
    """
    old_isolation = conn.isolation_level
    conn.isolation_level = None  # manual transaction control; DDL needs to be rollback-able too
    try:
        conn.execute(SCHEMA_MIGRATIONS_TABLE)
        applied_version = current_version(conn)
        newly_applied: list[int] = []
        for version, name, fn in MIGRATIONS:
            if version <= applied_version:
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(timezone.utc).isoformat()),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            newly_applied.append(version)
        return newly_applied
    finally:
        conn.isolation_level = old_isolation
