import sqlite3

import pytest

from devmemory import storage


def test_init_store_creates_db_and_records_dir(tmp_path):
    db_path = storage.init_store(tmp_path)

    assert db_path.exists()
    assert (tmp_path / ".devmemory" / "records").is_dir()

    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "decisions" in tables


def test_init_store_is_idempotent(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="s1")

    # Re-running init must not wipe existing data.
    storage.init_store(tmp_path)

    assert len(storage.query_decisions(tmp_path)) == 1


def test_add_decision_requires_init(tmp_path):
    with pytest.raises(FileNotFoundError):
        storage.add_decision(tmp_path, file="foo.py", summary="did a thing")


def test_add_decision_inserts_row_and_markdown_mirror(tmp_path):
    storage.init_store(tmp_path)

    record = storage.add_decision(
        tmp_path,
        file="src/foo.py",
        symbol="do_thing",
        summary="Switched to X",
        why="Y was slow",
        rejected="Kept Y",
        tags=["perf", "refactor"],
        session_ref="sess-1",
        author="kriti",
    )

    assert record["id"] == 1

    md_path = tmp_path / ".devmemory" / "records" / "000001.md"
    assert md_path.exists()
    content = md_path.read_text()
    assert "Switched to X" in content
    assert "Y was slow" in content
    assert "Kept Y" in content
    assert "do_thing" in content
    assert "src/foo.py" in content
    assert "perf,refactor" in content


def test_add_decision_auto_increments_ids(tmp_path):
    storage.init_store(tmp_path)

    r1 = storage.add_decision(tmp_path, file="a.py", summary="first")
    r2 = storage.add_decision(tmp_path, file="a.py", summary="second")

    assert r1["id"] == 1
    assert r2["id"] == 2
    assert (tmp_path / ".devmemory" / "records" / "000002.md").exists()


def test_query_decisions_filters_by_file_and_symbol(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="s1")
    storage.add_decision(tmp_path, file="a.py", symbol="bar", summary="s2")
    storage.add_decision(tmp_path, file="b.py", symbol="foo", summary="s3")

    by_file = storage.query_decisions(tmp_path, file="a.py")
    assert {r["summary"] for r in by_file} == {"s1", "s2"}

    by_symbol = storage.query_decisions(tmp_path, symbol="foo")
    assert {r["summary"] for r in by_symbol} == {"s1", "s3"}

    by_both = storage.query_decisions(tmp_path, file="a.py", symbol="foo")
    assert [r["summary"] for r in by_both] == ["s1"]


def test_query_decisions_symbol_is_substring_match(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="compute_total", summary="s1")

    assert len(storage.query_decisions(tmp_path, symbol="total")) == 1


def test_query_decisions_empty_store_returns_empty_list(tmp_path):
    storage.init_store(tmp_path)
    assert storage.query_decisions(tmp_path, file="nope.py") == []


def test_add_decision_defaults_to_accepted_status_and_developer_input_origin(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")

    assert record["status"] == "accepted"
    assert record["origin"] == "developer-input"
    assert record["superseded_by"] is None


def test_add_decision_infers_git_origin_from_commit_session_ref(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1", session_ref="commit:abc123")

    assert record["origin"] == "git"


def test_add_decision_rejects_invalid_status(tmp_path):
    storage.init_store(tmp_path)
    with pytest.raises(ValueError, match="invalid status"):
        storage.add_decision(tmp_path, file="a.py", summary="s1", status="bogus")

    assert storage.query_decisions(tmp_path) == []


def test_add_decision_writes_a_created_decision_history_row(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")

    history = storage.get_decision_history(tmp_path, record["id"])
    assert len(history) == 1
    assert history[0]["event"] == "created"
    assert history[0]["new_status"] == "accepted"


def test_set_decision_status_updates_status_and_appends_history(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")

    updated = storage.set_decision_status(tmp_path, record["id"], "deprecated", note="no longer true")

    assert updated["status"] == "deprecated"
    history = storage.get_decision_history(tmp_path, record["id"])
    assert history[-1]["event"] == "status_changed"
    assert history[-1]["old_status"] == "accepted"
    assert history[-1]["new_status"] == "deprecated"
    assert history[-1]["note"] == "no longer true"

    # Markdown mirror reflects the new status, not just the DB row.
    md_path = tmp_path / ".devmemory" / "records" / f"{record['id']:06d}.md"
    assert "deprecated" in md_path.read_text()


def test_set_decision_status_rejects_invalid_status(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")
    with pytest.raises(ValueError, match="invalid status"):
        storage.set_decision_status(tmp_path, record["id"], "bogus")


def test_set_decision_status_raises_for_unknown_decision(tmp_path):
    storage.init_store(tmp_path)
    with pytest.raises(ValueError, match="no decision #999"):
        storage.set_decision_status(tmp_path, 999, "deprecated")


def test_supersede_decision_flips_old_status_and_links_to_new(tmp_path):
    storage.init_store(tmp_path)
    old = storage.add_decision(tmp_path, file="a.py", summary="old approach")
    new = storage.add_decision(tmp_path, file="a.py", summary="new approach")

    updated_old = storage.supersede_decision(tmp_path, old["id"], new["id"], note="switched approach")

    assert updated_old["status"] == "superseded"
    assert updated_old["superseded_by"] == new["id"]

    history = storage.get_decision_history(tmp_path, old["id"])
    assert history[-1]["event"] == "superseded"
    assert history[-1]["new_status"] == "superseded"

    # The new decision itself is untouched.
    fresh_new = storage.query_decisions(tmp_path, file="a.py")
    new_row = next(r for r in fresh_new if r["id"] == new["id"])
    assert new_row["status"] == "accepted"
    assert new_row["superseded_by"] is None


def test_supersede_decision_never_deletes_the_old_row(tmp_path):
    storage.init_store(tmp_path)
    old = storage.add_decision(tmp_path, file="a.py", summary="old approach")
    new = storage.add_decision(tmp_path, file="a.py", summary="new approach")

    storage.supersede_decision(tmp_path, old["id"], new["id"])

    all_rows = storage.query_decisions(tmp_path, file="a.py")
    assert {r["id"] for r in all_rows} == {old["id"], new["id"]}


def test_supersede_decision_raises_for_unknown_ids(tmp_path):
    storage.init_store(tmp_path)
    existing = storage.add_decision(tmp_path, file="a.py", summary="s1")

    with pytest.raises(ValueError, match="no decision #999"):
        storage.supersede_decision(tmp_path, 999, existing["id"])
    with pytest.raises(ValueError, match="no decision #999"):
        storage.supersede_decision(tmp_path, existing["id"], 999)


def test_get_decision_history_empty_for_unknown_decision(tmp_path):
    storage.init_store(tmp_path)
    assert storage.get_decision_history(tmp_path, 999) == []


def test_add_decision_link_and_query_it_back(tmp_path):
    storage.init_store(tmp_path)
    link = storage.add_decision_link(tmp_path, "node", "42", "context_record", "7", "DOCUMENTED_BY")

    assert link["kind"] == "DOCUMENTED_BY"
    found = storage.get_decision_links(tmp_path, src_kind="node", src_ref="42")
    assert len(found) == 1
    assert found[0]["dst_kind"] == "context_record"
    assert found[0]["dst_ref"] == "7"


def test_supersede_decision_writes_a_supersedes_decision_link(tmp_path):
    storage.init_store(tmp_path)
    old = storage.add_decision(tmp_path, file="a.py", summary="old approach")
    new = storage.add_decision(tmp_path, file="a.py", summary="new approach")

    storage.supersede_decision(tmp_path, old["id"], new["id"])

    links = storage.get_decision_links(tmp_path, src_kind="decision", src_ref=str(new["id"]), dst_kind="decision")
    assert len(links) == 1
    assert links[0]["kind"] == "SUPERSEDES"
    assert links[0]["dst_ref"] == str(old["id"])


def test_add_decision_from_commit_session_ref_writes_introduced_by_link(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1", session_ref="commit:deadbeef")

    links = storage.get_decision_links(tmp_path, src_kind="decision", src_ref=str(record["id"]))
    assert len(links) == 1
    assert links[0]["kind"] == "INTRODUCED_BY"
    assert links[0]["dst_kind"] == "commit"
    assert links[0]["dst_ref"] == "deadbeef"


def test_add_decision_without_commit_session_ref_writes_no_introduced_by_link(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1", author="kriti")

    assert storage.get_decision_links(tmp_path, src_kind="decision", src_ref=str(record["id"])) == []


def test_log_since_filters_by_created_at(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(
        tmp_path, file="a.py", summary="old", created_at="2026-01-01T00:00:00+00:00"
    )
    storage.add_decision(
        tmp_path, file="a.py", summary="new", created_at="2026-10-01T00:00:00+00:00"
    )

    recent = storage.log_since(tmp_path, "2026-06-01")
    assert [r["summary"] for r in recent] == ["new"]

    everything = storage.log_since(tmp_path, "2020-01-01")
    assert {r["summary"] for r in everything} == {"old", "new"}

    none = storage.log_since(tmp_path, "2027-01-01")
    assert none == []
