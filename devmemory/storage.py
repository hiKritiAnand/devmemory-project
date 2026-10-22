"""SQLite-backed decision store, mirrored as plain Markdown files.

Records are keyed by file + symbol (not by date/session) so a query scoped
to the function being edited only returns relevant history. The SQLite DB
is the queryable index; each record is also written out as a Markdown file
under `.devmemory/records/` so the history stays human-readable and
grep-able without the CLI, and stays mergeable via git even though the DB
file itself isn't.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import git_utils, migrations

DB_RELATIVE_PATH = Path(".devmemory") / "memory.db"
RECORDS_RELATIVE_DIR = Path(".devmemory") / "records"

VALID_STATUSES = {"proposed", "accepted", "rejected", "deprecated", "superseded"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file TEXT NOT NULL,
    symbol TEXT,
    summary TEXT NOT NULL,
    why TEXT,
    rejected TEXT,
    tags TEXT,
    session_ref TEXT,
    author TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    file TEXT NOT NULL,
    symbol TEXT,
    language TEXT,
    content_hash TEXT,
    line_start INTEGER,
    line_end INTEGER,
    signature TEXT,
    docstring TEXT
);

-- SQLite treats NULL as distinct from NULL in a plain UNIQUE constraint, so
-- file/external nodes (symbol IS NULL) would never dedupe against a plain
-- UNIQUE(file, symbol). Index on COALESCE(symbol, '') instead so re-scanning
-- the same file/external target updates its existing row rather than
-- inserting a duplicate every time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_file_symbol
    ON nodes(file, COALESCE(symbol, ''));

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_node_id INTEGER NOT NULL REFERENCES nodes(id),
    dst_node_id INTEGER NOT NULL REFERENCES nodes(id),
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    accessed_at TEXT NOT NULL
);
"""

# FTS5 is a SQLite build-time option, not guaranteed on every machine
# devmemory runs on, so it's created separately from SCHEMA (which must
# always succeed) and wrapped in try/except at the call site in
# init_store(). has_fts5()/search_decisions() check for decisions_fts's
# existence rather than assuming it, so a build without FTS5 degrades to a
# plain LIKE search instead of crashing.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
    summary, why, rejected, tags, content='decisions', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS decisions_ai AFTER INSERT ON decisions BEGIN
    INSERT INTO decisions_fts(rowid, summary, why, rejected, tags)
    VALUES (new.id, new.summary, new.why, new.rejected, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS decisions_ad AFTER DELETE ON decisions BEGIN
    INSERT INTO decisions_fts(decisions_fts, rowid, summary, why, rejected, tags)
    VALUES ('delete', old.id, old.summary, old.why, old.rejected, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS decisions_au AFTER UPDATE ON decisions BEGIN
    INSERT INTO decisions_fts(decisions_fts, rowid, summary, why, rejected, tags)
    VALUES ('delete', old.id, old.summary, old.why, old.rejected, old.tags);
    INSERT INTO decisions_fts(rowid, summary, why, rejected, tags)
    VALUES (new.id, new.summary, new.why, new.rejected, new.tags);
END;
"""


def get_db_path(root) -> Path:
    return Path(root) / DB_RELATIVE_PATH


def get_records_dir(root) -> Path:
    return Path(root) / RECORDS_RELATIVE_DIR


def init_store(root) -> Path:
    """Create the SQLite store and records directory if they don't exist yet."""
    db_path = get_db_path(root)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    get_records_dir(root).mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        try:
            conn.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            pass  # this SQLite build lacks FTS5; search_decisions() falls back to LIKE
        conn.commit()
        migrations.apply_migrations(conn)
    finally:
        conn.close()
    return db_path


def _connect(root) -> sqlite3.Connection:
    db_path = get_db_path(root)
    if not db_path.exists():
        raise FileNotFoundError(
            f"devmemory store not initialized at {db_path}. Run `devmemory init` first."
        )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Self-heals a database that predates a schema change (or predates
    # migrations.py itself) on every connection, not just at `init`/`migrate`.
    migrations.apply_migrations(conn)
    return conn


def _markdown_path(root, record_id: int) -> Path:
    return get_records_dir(root) / f"{record_id:06d}.md"


def _render_markdown(record: dict) -> str:
    lines = [
        f"# Decision {record['id']}",
        "",
        f"- **File:** {record['file']}",
    ]
    if record.get("symbol"):
        lines.append(f"- **Symbol:** {record['symbol']}")
    lines.append(f"- **Created:** {record['created_at']}")
    if record.get("author"):
        lines.append(f"- **Author:** {record['author']}")
    if record.get("session_ref"):
        lines.append(f"- **Session:** {record['session_ref']}")
    if record.get("tags"):
        lines.append(f"- **Tags:** {record['tags']}")

    if record.get("status") and record["status"] != "accepted":
        status_line = f"- **Status:** {record['status']}"
        if record.get("superseded_by"):
            status_line += f" (superseded by #{record['superseded_by']})"
        lines.append(status_line)
    if record.get("owner"):
        lines.append(f"- **Owner:** {record['owner']}")
    if record.get("scope"):
        lines.append(f"- **Scope:** {record['scope']}")

    lines += ["", "## Summary", "", record["summary"]]
    if record.get("why"):
        lines += ["", "## Why", "", record["why"]]
    if record.get("rejected"):
        lines += ["", "## Rejected alternatives", "", record["rejected"]]
    if record.get("alternatives"):
        lines += ["", "## Alternatives considered", "", record["alternatives"]]
    if record.get("consequences"):
        lines += ["", "## Consequences", "", record["consequences"]]

    return "\n".join(lines) + "\n"


def _write_markdown_mirror(root, record: dict) -> Path:
    path = _markdown_path(root, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_markdown(record), encoding="utf-8")
    return path


def add_decision(
    root,
    file: str,
    summary: str,
    symbol: str | None = None,
    why: str | None = None,
    rejected: str | None = None,
    tags=None,
    session_ref: str | None = None,
    author: str | None = None,
    created_at: str | None = None,
    status: str = "accepted",
    alternatives: str | None = None,
    consequences: str | None = None,
    scope: str | None = None,
    owner: str | None = None,
    origin: str | None = None,
    global_: bool = False,
) -> dict:
    """Insert a decision record and mirror it as Markdown. Returns the record dict.

    `origin` defaults to 'git' when `session_ref` follows the existing
    "commit:<sha>" convention set by extract.py's git-hook path, else
    'developer-input' -- mirroring the same inference the Phase-2 migration
    backfill applies to pre-existing rows.

    The decision is stamped with the current git branch unless `global_=True`
    forces it project-wide (branch=NULL) -- outside a git repo (or on
    detached HEAD), it's always project-wide, since there's no branch to
    scope it to.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(VALID_STATUSES)}")

    created_at = created_at or datetime.now(timezone.utc).isoformat()
    tags_str = ",".join(tags) if isinstance(tags, (list, tuple)) else (tags or "")
    resolved_origin = origin or ("git" if (session_ref or "").startswith("commit:") else "developer-input")
    branch = None if global_ else git_utils.current_branch(root)

    conn = _connect(root)
    try:
        cur = conn.execute(
            """
            INSERT INTO decisions
                (file, symbol, summary, why, rejected, tags, session_ref, author, created_at,
                 status, alternatives, consequences, scope, owner, origin, updated_at, branch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file, symbol, summary, why, rejected, tags_str, session_ref, author, created_at,
                status, alternatives, consequences, scope, owner, resolved_origin, created_at, branch,
            ),
        )
        record_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO decision_history (decision_id, event, old_status, new_status, note, changed_by, changed_at)
            VALUES (?, 'created', NULL, ?, NULL, ?, ?)
            """,
            (record_id, status, author, created_at),
        )
        if resolved_origin == "git" and session_ref:
            commit_sha = session_ref.removeprefix("commit:")
            conn.execute(
                """
                INSERT INTO decision_links
                    (src_kind, src_ref, dst_kind, dst_ref, kind, origin, confidence, verification_status, created_at)
                VALUES ('decision', ?, 'commit', ?, 'INTRODUCED_BY', 'source', 1.0, 'current', ?)
                """,
                (str(record_id), commit_sha, created_at),
            )
        conn.commit()
    finally:
        conn.close()

    record = {
        "id": record_id,
        "file": file,
        "symbol": symbol,
        "summary": summary,
        "why": why,
        "rejected": rejected,
        "tags": tags_str,
        "session_ref": session_ref,
        "author": author,
        "created_at": created_at,
        "status": status,
        "alternatives": alternatives,
        "consequences": consequences,
        "scope": scope,
        "owner": owner,
        "superseded_by": None,
        "origin": resolved_origin,
        "confidence": 1.0,
        "verification_status": "current",
        "updated_at": created_at,
        "branch": branch,
    }
    _write_markdown_mirror(root, record)
    return record


def _branch_filter(root, all_branches: bool) -> tuple[str, list]:
    """A SQL clause + params scoping a decisions query to the current branch
    (project-wide rows, branch IS NULL, always included -- never silently
    hidden) unless `all_branches=True`. Shared by query_decisions/
    search_decisions/log_since so the three stay consistent.
    """
    if all_branches:
        return "", []
    return "(branch IS NULL OR branch = ?)", [git_utils.current_branch(root)]


def _get_decision_row(conn, decision_id: int) -> dict:
    row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        raise ValueError(f"no decision #{decision_id}")
    return dict(row)


def set_decision_status(
    root, decision_id: int, new_status: str, note: str | None = None, changed_by: str | None = None
) -> dict:
    """Changes a decision's status in place and appends a `decision_history`
    row recording the transition. Never deletes anything."""
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status {new_status!r}; must be one of {sorted(VALID_STATUSES)}")

    conn = _connect(root)
    try:
        old = _get_decision_row(conn, decision_id)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE decisions SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, decision_id),
        )
        conn.execute(
            """
            INSERT INTO decision_history (decision_id, event, old_status, new_status, note, changed_by, changed_at)
            VALUES (?, 'status_changed', ?, ?, ?, ?, ?)
            """,
            (decision_id, old["status"], new_status, note, changed_by, now),
        )
        conn.commit()
        updated = _get_decision_row(conn, decision_id)
    finally:
        conn.close()
    _write_markdown_mirror(root, updated)
    return updated


def supersede_decision(
    root, old_decision_id: int, new_decision_id: int, note: str | None = None, changed_by: str | None = None
) -> dict:
    """Marks `old_decision_id` as superseded by `new_decision_id`. The old
    row is never deleted -- its status flips to 'superseded',
    `superseded_by` points at the new decision, and a `decision_history` row
    preserves the transition for audit purposes.
    """
    conn = _connect(root)
    try:
        old = _get_decision_row(conn, old_decision_id)
        _get_decision_row(conn, new_decision_id)  # raises ValueError if it doesn't exist
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE decisions SET status = 'superseded', superseded_by = ?, updated_at = ? WHERE id = ?",
            (new_decision_id, now, old_decision_id),
        )
        conn.execute(
            """
            INSERT INTO decision_history (decision_id, event, old_status, new_status, note, changed_by, changed_at)
            VALUES (?, 'superseded', ?, 'superseded', ?, ?, ?)
            """,
            (old_decision_id, old["status"], note, changed_by, now),
        )
        conn.execute(
            """
            INSERT INTO decision_links
                (src_kind, src_ref, dst_kind, dst_ref, kind, origin, confidence, verification_status, created_at)
            VALUES ('decision', ?, 'decision', ?, 'SUPERSEDES', 'source', 1.0, 'current', ?)
            """,
            (str(new_decision_id), str(old_decision_id), now),
        )
        conn.commit()
        updated = _get_decision_row(conn, old_decision_id)
    finally:
        conn.close()
    _write_markdown_mirror(root, updated)
    return updated


def get_decision_history(root, decision_id: int) -> list[dict]:
    conn = _connect(root)
    try:
        rows = conn.execute(
            "SELECT * FROM decision_history WHERE decision_id = ? ORDER BY changed_at ASC",
            (decision_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def upsert_context_record(
    root, kind: str, source_path: str, content: str, title: str | None = None,
    scope_path: str | None = None, origin: str = "onboarding", confidence: float | None = None,
    branch: str | None = None, content_hash: str | None = None,
) -> dict:
    """Insert or update (keyed on `source_path`, the unique index) a
    context-record source. Always overwrites `content`/`content_hash` on an
    update -- callers (onboard.py) are expected to skip calling this at all
    when the content hash hasn't changed, rather than relying on this
    function to no-op; it always writes what it's given.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(root)
    try:
        existing = conn.execute(
            "SELECT id, created_at FROM context_records WHERE source_path = ?", (source_path,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE context_records
                SET kind = ?, title = ?, content = ?, scope_path = ?, origin = ?,
                    confidence = ?, branch = ?, content_hash = ?, updated_at = ?
                WHERE source_path = ?
                """,
                (kind, title, content, scope_path, origin, confidence, branch, content_hash, now, source_path),
            )
            record_id = existing["id"]
        else:
            conn.execute(
                """
                INSERT INTO context_records
                    (kind, source_path, title, content, scope_path, origin, confidence,
                     verification_status, branch, content_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?, ?)
                """,
                (kind, source_path, title, content, scope_path, origin, confidence, branch, content_hash, now, now),
            )
            record_id = conn.execute(
                "SELECT id FROM context_records WHERE source_path = ?", (source_path,)
            ).fetchone()["id"]
        conn.commit()
        row = conn.execute("SELECT * FROM context_records WHERE id = ?", (record_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_context_record_hashes(root) -> dict[str, str]:
    conn = _connect(root)
    try:
        return {
            r["source_path"]: r["content_hash"]
            for r in conn.execute("SELECT source_path, content_hash FROM context_records")
        }
    finally:
        conn.close()


def query_context_records(root, kind: str | None = None, scope_path: str | None = None) -> list[dict]:
    conn = _connect(root)
    try:
        clauses: list[str] = []
        params: list = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if scope_path is not None:
            clauses.append("scope_path = ?")
            params.append(scope_path)
        sql = "SELECT * FROM context_records"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY source_path ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def add_decision_link(
    root,
    src_kind: str,
    src_ref: str,
    dst_kind: str,
    dst_ref: str,
    kind: str,
    origin: str = "model-inference",
    confidence: float | None = None,
) -> dict:
    """Records a relationship that isn't node<->node (those live in `edges`;
    see relationships.py's registry for the full disposition of every
    relationship kind). `src_ref`/`dst_ref` are stringified ids/shas, matching
    the existing `access_log.ref_kind`/`ref_id` convention's spirit.
    """
    created_at = datetime.now(timezone.utc).isoformat()
    conn = _connect(root)
    try:
        cur = conn.execute(
            """
            INSERT INTO decision_links
                (src_kind, src_ref, dst_kind, dst_ref, kind, origin, confidence, verification_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?)
            """,
            (src_kind, str(src_ref), dst_kind, str(dst_ref), kind, origin, confidence, created_at),
        )
        conn.commit()
        link_id = cur.lastrowid
    finally:
        conn.close()
    return {
        "id": link_id, "src_kind": src_kind, "src_ref": str(src_ref),
        "dst_kind": dst_kind, "dst_ref": str(dst_ref), "kind": kind,
        "origin": origin, "confidence": confidence, "verification_status": "current",
        "created_at": created_at,
    }


def get_decision_links(root, src_kind: str | None = None, src_ref: str | None = None,
                        dst_kind: str | None = None, dst_ref: str | None = None) -> list[dict]:
    conn = _connect(root)
    try:
        clauses, params = [], []
        if src_kind is not None:
            clauses.append("src_kind = ?")
            params.append(src_kind)
        if src_ref is not None:
            clauses.append("src_ref = ?")
            params.append(str(src_ref))
        if dst_kind is not None:
            clauses.append("dst_kind = ?")
            params.append(dst_kind)
        if dst_ref is not None:
            clauses.append("dst_ref = ?")
            params.append(str(dst_ref))
        sql = "SELECT * FROM decision_links"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def query_decisions(
    root, file: str | None = None, symbol: str | None = None, all_branches: bool = False
) -> list[dict]:
    """Return decisions matching an exact file path and/or a substring symbol
    match, scoped to the current branch (plus every project-wide, branch=NULL
    decision) unless `all_branches=True`."""
    conn = _connect(root)
    try:
        clauses = []
        params: list[str] = []
        if file:
            clauses.append("file = ?")
            params.append(file)
        if symbol:
            clauses.append("symbol LIKE ?")
            params.append(f"%{symbol}%")
        branch_clause, branch_params = _branch_filter(root, all_branches)
        if branch_clause:
            clauses.append(branch_clause)
            params.extend(branch_params)

        sql = "SELECT * FROM decisions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def log_since(root, since: str, all_branches: bool = False) -> list[dict]:
    """Return decisions created at or after `since` (an ISO date or datetime prefix).

    This is a plain metadata read straight from SQLite with no LLM call
    involved. Branch-scoped like query_decisions(), unless `all_branches=True`.
    """
    conn = _connect(root)
    try:
        branch_clause, branch_params = _branch_filter(root, all_branches)
        sql = "SELECT * FROM decisions WHERE created_at >= ?"
        params = [since]
        if branch_clause:
            sql += " AND " + branch_clause
            params.extend(branch_params)
        sql += " ORDER BY created_at ASC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def log_access(root, ref_kind: str, ref_ids, command: str, estimated_tokens: int | None = None) -> None:
    """Record that a read command actually surfaced these records/nodes to
    whoever (human or agent) called it. Call only when ref_ids is non-empty --
    there's nothing to log for a query that returned no results. This is the
    raw data `roi_report` reads back: whether devmemory's automatic, costly
    recording mechanisms are ever queried again, or just accumulate unread.

    `estimated_tokens` is only ever set by a caller that actually ran its
    output through compiler.compile_context (CLI `--compile` or an MCP tool
    call) -- a plain uncompiled read passes None, logged as NULL, never a
    fabricated zero.
    """
    if not ref_ids:
        return
    accessed_at = datetime.now(timezone.utc).isoformat()
    conn = _connect(root)
    try:
        conn.executemany(
            "INSERT INTO access_log (ref_kind, ref_id, command, accessed_at, estimated_tokens) VALUES (?, ?, ?, ?, ?)",
            [(ref_kind, ref_id, command, accessed_at, estimated_tokens) for ref_id in ref_ids],
        )
        conn.commit()
    finally:
        conn.close()


def roi_report(root, since: str | None = None) -> dict:
    """Bucket every decision by how it was created -- automatic git-hook LLM
    extraction (`session_ref` starting with "commit:", the one genuinely
    costly path) vs. everything else (manual `record` calls or the free
    Stop-hook nudge) -- and report, per bucket, how many have ever been
    queried again via `query`/`log`/`inspect`/`impact`, plus the oldest
    never-accessed record as a concrete pruning candidate.

    Also reports real token-cost evidence per bucket -- `total_estimated_tokens`
    and `avg_estimated_tokens_per_access`, computed only over access_log rows
    that actually have an estimate (a caller that used compiler.compile_context,
    via CLI `--compile` or an MCP tool call) -- `token_sample_count` states how
    many such rows existed, so a zero/None average is never confused with "no
    tokens were used" when really no estimate was ever recorded. `since`
    (an ISO date/datetime prefix) scopes the token-cost section to accesses
    at or after that date, independent of the creation-source bucketing.
    """
    conn = _connect(root)
    try:
        rows = conn.execute("SELECT id, session_ref, created_at FROM decisions").fetchall()
        accessed_ids = {
            row["ref_id"]
            for row in conn.execute("SELECT DISTINCT ref_id FROM access_log WHERE ref_kind = 'decision'")
        }

        token_sql = "SELECT ref_id, estimated_tokens FROM access_log WHERE ref_kind = 'decision' AND estimated_tokens IS NOT NULL"
        token_params: list = []
        if since:
            token_sql += " AND accessed_at >= ?"
            token_params.append(since)
        token_rows = conn.execute(token_sql, token_params).fetchall()

        decision_bucket: dict[int, str] = {}
        buckets: dict[str, dict] = {}
        for row in rows:
            bucket = "automatic_commit_extraction" if (row["session_ref"] or "").startswith("commit:") else "manual_or_session"
            decision_bucket[row["id"]] = bucket
            b = buckets.setdefault(bucket, {
                "total": 0, "accessed": 0, "oldest_unaccessed": None,
                "total_estimated_tokens": 0, "token_sample_count": 0,
            })
            b["total"] += 1
            if row["id"] in accessed_ids:
                b["accessed"] += 1
            elif b["oldest_unaccessed"] is None or row["created_at"] < b["oldest_unaccessed"]:
                b["oldest_unaccessed"] = row["created_at"]

        for t in token_rows:
            bucket = decision_bucket.get(t["ref_id"])
            if bucket is None:
                continue
            b = buckets[bucket]
            b["total_estimated_tokens"] += t["estimated_tokens"]
            b["token_sample_count"] += 1

        for b in buckets.values():
            b["avg_estimated_tokens_per_access"] = (
                b["total_estimated_tokens"] / b["token_sample_count"] if b["token_sample_count"] else None
            )
        return buckets
    finally:
        conn.close()


def has_fts5(root) -> bool:
    conn = _connect(root)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'decisions_fts'"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def search_decisions(root, text: str, all_branches: bool = False) -> list[dict]:
    """Full-text search across summary/why/rejected/tags -- the gap plain
    query_decisions() has, since that only matches an exact file and a
    substring symbol, not concept-level text like "payment retries". Uses
    FTS5 (ranked via bm25) when this SQLite build has it, otherwise a plain
    multi-term LIKE OR-search ordered by recency -- never a vector index;
    agentic/lexical search is the current baseline for code-adjacent text,
    and code/text embeddings are often weaker than expected anyway.
    Branch-scoped like query_decisions(), unless `all_branches=True`.
    """
    conn = _connect(root)
    try:
        branch_clause, branch_params = _branch_filter(root, all_branches)
        if has_fts5(root):
            sql = """
                SELECT d.* FROM decisions_fts
                JOIN decisions d ON d.id = decisions_fts.rowid
                WHERE decisions_fts MATCH ?
            """
            params = [text]
            if branch_clause:
                sql += " AND " + branch_clause.replace("branch", "d.branch")
                params.extend(branch_params)
            sql += " ORDER BY bm25(decisions_fts)"
            rows = conn.execute(sql, params).fetchall()
        else:
            terms = text.split()
            if not terms:
                return []
            clause = "(summary LIKE ? OR why LIKE ? OR rejected LIKE ? OR tags LIKE ?)"
            sql = "SELECT * FROM decisions WHERE " + " OR ".join([clause] * len(terms))
            params = [p for term in terms for p in (f"%{term}%",) * 4]
            if branch_clause:
                sql += " AND " + branch_clause
                params.extend(branch_params)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
