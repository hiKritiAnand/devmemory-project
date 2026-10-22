import sqlite3

import pytest

from devmemory import migrations, storage


def test_fresh_db_reaches_latest_version(tmp_path):
    storage.init_store(tmp_path)

    conn = sqlite3.connect(storage.get_db_path(tmp_path))
    try:
        assert migrations.current_version(conn) == migrations.MIGRATIONS[-1][0]
    finally:
        conn.close()


def test_apply_migrations_is_idempotent(tmp_path):
    storage.init_store(tmp_path)

    conn = sqlite3.connect(storage.get_db_path(tmp_path))
    try:
        first = migrations.apply_migrations(conn)
        second = migrations.apply_migrations(conn)
    finally:
        conn.close()

    assert second == []
    assert first == [] or all(v not in second for v in first)


def test_cli_migrate_twice_is_a_noop(tmp_path):
    from devmemory import cli

    storage.init_store(tmp_path)
    parser = cli.build_parser()

    args = parser.parse_args(["--path", str(tmp_path), "migrate"])
    args.func(args)  # first run may apply pending migrations (none pending here, but exercised)
    args2 = parser.parse_args(["--path", str(tmp_path), "migrate"])
    args2.func(args2)  # should not raise, and should be a no-op


def test_migration_preserves_existing_data_on_a_pre_migrations_database(tmp_path):
    """Simulate a database created before migrations.py existed: only the
    baseline SCHEMA, no schema_migrations table, with real decision rows
    already in it. Applying migrations must never lose that data.
    """
    db_path = storage.get_db_path(tmp_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    storage.get_records_dir(tmp_path).mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(storage.SCHEMA)
        conn.execute(
            "INSERT INTO decisions (file, summary, created_at) VALUES (?, ?, ?)",
            ("a.py", "pre-existing decision", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    conn = sqlite3.connect(db_path)
    try:
        migrations.apply_migrations(conn)
        rows = conn.execute("SELECT summary FROM decisions").fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == ["pre-existing decision"]


def test_failed_migration_rolls_back_and_does_not_advance_version(tmp_path, monkeypatch):
    storage.init_store(tmp_path)

    def _boom(conn):
        conn.execute("CREATE TABLE should_not_persist (x INTEGER)")
        raise RuntimeError("simulated migration failure")

    base_version = migrations.MIGRATIONS[-1][0]
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        migrations.MIGRATIONS + [(base_version + 1, "broken", _boom)],
    )

    conn = sqlite3.connect(storage.get_db_path(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            migrations.apply_migrations(conn)

        assert migrations.current_version(conn) == base_version
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "should_not_persist" not in tables
    finally:
        conn.close()


def test_has_column_and_add_column_if_missing(tmp_path):
    storage.init_store(tmp_path)
    conn = sqlite3.connect(storage.get_db_path(tmp_path))
    try:
        assert migrations.has_column(conn, "decisions", "summary") is True
        assert migrations.has_column(conn, "decisions", "nonexistent_col") is False

        migrations.add_column_if_missing(conn, "decisions", "scratch_col", "scratch_col TEXT")
        assert migrations.has_column(conn, "decisions", "scratch_col") is True

        # Calling it again must not raise (SQLite has no ADD COLUMN IF NOT EXISTS).
        migrations.add_column_if_missing(conn, "decisions", "scratch_col", "scratch_col TEXT")
    finally:
        conn.close()
