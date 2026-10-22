from devmemory import storage
from devmemory.cli import main


def run(capsys, argv):
    main(argv)
    return capsys.readouterr().out


def test_log_access_noop_on_empty_ids(tmp_path):
    storage.init_store(tmp_path)
    storage.log_access(tmp_path, "decision", [], "query")  # must not raise or insert anything
    conn = storage._connect(tmp_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM access_log").fetchone()[0] == 0
    finally:
        conn.close()


def test_roi_report_buckets_by_session_ref_prefix(tmp_path):
    storage.init_store(tmp_path)
    auto = storage.add_decision(tmp_path, file="a.py", summary="auto one", session_ref="commit:abc123")
    manual = storage.add_decision(tmp_path, file="b.py", summary="manual one", session_ref="sess-1")
    no_session = storage.add_decision(tmp_path, file="c.py", summary="no session")

    storage.log_access(tmp_path, "decision", [auto["id"]], "query")

    report = storage.roi_report(tmp_path)

    assert report["automatic_commit_extraction"]["total"] == 1
    assert report["automatic_commit_extraction"]["accessed"] == 1
    assert report["manual_or_session"]["total"] == 2  # manual + no_session bucketed together
    assert report["manual_or_session"]["accessed"] == 0
    assert report["manual_or_session"]["oldest_unaccessed"] is not None


def test_roi_report_oldest_unaccessed_is_the_earliest_created(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="old", created_at="2026-01-01T00:00:00+00:00")
    storage.add_decision(tmp_path, file="b.py", summary="new", created_at="2026-06-01T00:00:00+00:00")

    report = storage.roi_report(tmp_path)
    assert report["manual_or_session"]["oldest_unaccessed"] == "2026-01-01T00:00:00+00:00"


def test_cli_roi_reports_hit_rate(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, [
        "--path", str(tmp_path), "record",
        "--file", "a.py", "--summary", "s1", "--session", "commit:abc",
    ])
    run(capsys, [
        "--path", str(tmp_path), "record",
        "--file", "b.py", "--summary", "s2",
    ])

    # Query once so one of the two records is marked accessed.
    run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py"])

    out = run(capsys, ["--path", str(tmp_path), "roi"])
    assert "Automatic" in out
    assert "1/1 queried again since creation (100%)" in out
    assert "Manual" in out
    assert "0/1 queried again since creation (0%)" in out


def test_cli_roi_reports_no_decisions_yet(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    out = run(capsys, ["--path", str(tmp_path), "roi"])
    assert "No decisions recorded yet." in out


def test_cli_query_logs_access(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])
    run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py"])

    conn = storage._connect(tmp_path)
    try:
        rows = conn.execute("SELECT command FROM access_log").fetchall()
    finally:
        conn.close()
    assert [r["command"] for r in rows] == ["query"]


def test_roi_report_token_cost_is_none_when_no_compiled_access_recorded(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")
    storage.log_access(tmp_path, "decision", [record["id"]], "query")  # plain, uncompiled access -- no estimate

    report = storage.roi_report(tmp_path)

    assert report["manual_or_session"]["token_sample_count"] == 0
    assert report["manual_or_session"]["total_estimated_tokens"] == 0
    assert report["manual_or_session"]["avg_estimated_tokens_per_access"] is None


def test_roi_report_aggregates_real_token_estimates_by_bucket(tmp_path):
    storage.init_store(tmp_path)
    auto = storage.add_decision(tmp_path, file="a.py", summary="s1", session_ref="commit:abc")
    manual = storage.add_decision(tmp_path, file="b.py", summary="s2")

    storage.log_access(tmp_path, "decision", [auto["id"]], "mcp_search", estimated_tokens=50)
    storage.log_access(tmp_path, "decision", [manual["id"]], "query", estimated_tokens=10)
    storage.log_access(tmp_path, "decision", [manual["id"]], "query", estimated_tokens=30)

    report = storage.roi_report(tmp_path)

    assert report["automatic_commit_extraction"]["token_sample_count"] == 1
    assert report["automatic_commit_extraction"]["total_estimated_tokens"] == 50
    assert report["automatic_commit_extraction"]["avg_estimated_tokens_per_access"] == 50

    assert report["manual_or_session"]["token_sample_count"] == 2
    assert report["manual_or_session"]["total_estimated_tokens"] == 40
    assert report["manual_or_session"]["avg_estimated_tokens_per_access"] == 20


def test_roi_report_since_scopes_only_the_token_cost_section(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")

    conn = storage._connect(tmp_path)
    try:
        conn.execute(
            "INSERT INTO access_log (ref_kind, ref_id, command, accessed_at, estimated_tokens) VALUES (?, ?, ?, ?, ?)",
            ("decision", record["id"], "query", "2026-01-01T00:00:00+00:00", 100),
        )
        conn.execute(
            "INSERT INTO access_log (ref_kind, ref_id, command, accessed_at, estimated_tokens) VALUES (?, ?, ?, ?, ?)",
            ("decision", record["id"], "query", "2026-10-01T00:00:00+00:00", 20),
        )
        conn.commit()
    finally:
        conn.close()

    all_time = storage.roi_report(tmp_path)
    assert all_time["manual_or_session"]["token_sample_count"] == 2

    scoped = storage.roi_report(tmp_path, since="2026-06-01")
    assert scoped["manual_or_session"]["token_sample_count"] == 1
    assert scoped["manual_or_session"]["total_estimated_tokens"] == 20
    # The hit-rate bucketing itself (accessed/total) is unaffected by --since.
    assert scoped["manual_or_session"]["accessed"] == all_time["manual_or_session"]["accessed"]


def test_cli_roi_prints_token_cost_section_when_compiled_access_exists(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])
    run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py", "--compile"])

    out = run(capsys, ["--path", str(tmp_path), "roi"])
    assert "token cost" in out
    assert "estimated token(s)" in out


def test_cli_roi_since_flag_is_accepted(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "roi", "--since", "2026-01-01"])
    assert "Manual" in out


def test_cli_log_logs_access(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])
    run(capsys, ["--path", str(tmp_path), "log", "--since", "2020-01-01"])

    conn = storage._connect(tmp_path)
    try:
        rows = conn.execute("SELECT command FROM access_log").fetchall()
    finally:
        conn.close()
    assert [r["command"] for r in rows] == ["log"]
