from devmemory import storage


def test_has_fts5_true_after_normal_init(tmp_path):
    storage.init_store(tmp_path)
    assert storage.has_fts5(tmp_path) is True


def test_search_decisions_matches_text_in_why_field_not_file_or_symbol(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(
        tmp_path, file="billing/invoice.py", symbol="calculate_total",
        summary="Switched to Decimal", why="float rounding caused payment retries to fail silently",
    )
    storage.add_decision(tmp_path, file="unrelated.py", summary="Something else entirely", why="no overlap here")

    results = storage.search_decisions(tmp_path, "payment retries")

    assert len(results) == 1
    assert results[0]["file"] == "billing/invoice.py"


def test_search_decisions_no_match_returns_empty(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="s1", why="nothing relevant")

    assert storage.search_decisions(tmp_path, "nonexistent_term_xyz") == []


def test_search_decisions_multi_word_query_requires_both_terms_with_fts5(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="s1", why="payment retry logic lives here")
    storage.add_decision(tmp_path, file="b.py", summary="s2", why="retry logic for network calls")

    results = storage.search_decisions(tmp_path, "payment retry")
    assert {r["file"] for r in results} == {"a.py"}


def test_search_decisions_falls_back_to_like_when_fts5_unavailable(tmp_path, monkeypatch):
    storage.init_store(tmp_path)
    storage.add_decision(
        tmp_path, file="billing/invoice.py", symbol="calculate_total",
        summary="Switched to Decimal", why="float rounding caused payment retries to fail silently",
    )
    storage.add_decision(tmp_path, file="unrelated.py", summary="Something else entirely", why="no overlap here")

    monkeypatch.setattr(storage, "has_fts5", lambda root: False)
    results = storage.search_decisions(tmp_path, "payment")

    assert len(results) == 1
    assert results[0]["file"] == "billing/invoice.py"


def test_search_decisions_like_fallback_returns_same_shape_as_fts5(tmp_path, monkeypatch):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="s1", why="payment issue")

    fts_results = storage.search_decisions(tmp_path, "payment")
    monkeypatch.setattr(storage, "has_fts5", lambda root: False)
    like_results = storage.search_decisions(tmp_path, "payment")

    assert fts_results[0].keys() == like_results[0].keys()


def test_cli_query_text_searches_and_logs_access(tmp_path, capsys):
    from devmemory.cli import main

    main(["--path", str(tmp_path), "init"])
    capsys.readouterr()
    main([
        "--path", str(tmp_path), "record",
        "--file", "billing/invoice.py", "--summary", "s1", "--why", "payment retry handling",
    ])
    capsys.readouterr()

    main(["--path", str(tmp_path), "query", "--text", "payment"])
    out = capsys.readouterr().out

    assert "billing/invoice.py" in out
    conn = storage._connect(tmp_path)
    try:
        rows = conn.execute("SELECT command FROM access_log").fetchall()
    finally:
        conn.close()
    assert [r["command"] for r in rows] == ["query"]
