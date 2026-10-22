import sys

from devmemory import conflicts, storage


def _write_stub(tmp_path, body, name="stub.py"):
    stub = tmp_path / name
    stub.write_text(body)
    return [sys.executable, str(stub)]


def test_no_conflicts_for_a_single_decision(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use retries")

    assert conflicts.detect_conflicts(tmp_path, "a.py", "foo") == []


def test_status_overlap_flags_an_active_pair_with_no_text_contradiction(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Cache results in Redis", status="accepted")
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Add a timeout to the handler", status="proposed")

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo")

    assert len(found) == 1
    assert found[0].rule == "status_overlap"
    assert found[0].confidence == "high"


def test_keyword_negation_flags_contradictory_active_decisions_as_medium(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(
        tmp_path, file="a.py", symbol="foo",
        summary="Use synchronous retries for payment calls", status="accepted",
    )
    storage.add_decision(
        tmp_path, file="a.py", symbol="foo",
        summary="Never use synchronous retries, switch to an async queue", status="accepted",
    )

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo")

    assert len(found) == 1
    assert found[0].rule == "keyword_negation"
    assert found[0].confidence == "medium"


def test_keyword_negation_requires_exactly_one_side_negating(tmp_path):
    storage.init_store(tmp_path)
    # Both plainly asserting -- no negation anywhere -- should fall back to
    # status_overlap, not keyword_negation.
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use retries", status="accepted")
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use retries with backoff", status="accepted")

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo")
    assert len(found) == 1
    assert found[0].rule == "status_overlap"


def test_keyword_negation_requires_an_inactive_decision_to_be_excluded(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(
        tmp_path, file="a.py", symbol="foo", summary="Use synchronous retries", status="deprecated",
    )
    storage.add_decision(
        tmp_path, file="a.py", symbol="foo", summary="Never use synchronous retries", status="accepted",
    )

    # One side is deprecated (inactive), so neither rule should fire --
    # there's no live conflict between a retired decision and a current one.
    assert conflicts.detect_conflicts(tmp_path, "a.py", "foo") == []


def test_already_superseded_pair_is_never_flagged(tmp_path):
    storage.init_store(tmp_path)
    old = storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use approach A")
    new = storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use approach B")
    storage.supersede_decision(tmp_path, old["id"], new["id"])

    assert conflicts.detect_conflicts(tmp_path, "a.py", "foo") == []


def test_scope_based_overlap_without_exact_file_match(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="src/cache/README.md", summary="Use Redis", scope="src/cache/")
    storage.add_decision(tmp_path, file="src/cache/README.md", summary="Add a timeout", scope="src/cache/")

    found = conflicts.detect_conflicts(tmp_path, "src/cache/impl.py")
    assert len(found) == 1
    assert found[0].rule == "status_overlap"


def test_llm_arbitrate_never_invoked_unless_passed_explicitly(tmp_path, monkeypatch):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use synchronous retries", status="accepted")
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Never use synchronous retries", status="accepted")

    def crash_if_called(*args, **kwargs):
        raise AssertionError("LLM must not be invoked without --llm-arbitrate / llm_arbitrate=True")

    from devmemory import extract
    monkeypatch.setattr(extract, "run_llm", crash_if_called)

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo")  # llm_arbitrate defaults to False
    assert len(found) == 1
    assert found[0].rule == "keyword_negation"


def test_llm_arbitrate_confirming_a_conflict_raises_confidence_to_high(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use synchronous retries", status="accepted")
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Never use synchronous retries", status="accepted")

    stub_command = _write_stub(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'conflicts': True, 'explanation': 'directly contradictory'}))\n",
    )

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo", llm_arbitrate=True, llm_command=stub_command)
    assert len(found) == 1
    assert found[0].confidence == "high"
    assert found[0].reason == "directly contradictory"


def test_llm_arbitrate_rejecting_drops_the_conflict(tmp_path):
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Use synchronous retries", status="accepted")
    storage.add_decision(tmp_path, file="a.py", symbol="foo", summary="Never use synchronous retries", status="accepted")

    stub_command = _write_stub(
        tmp_path,
        "import sys, json\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'conflicts': False, 'explanation': 'different subsystems'}))\n",
    )

    found = conflicts.detect_conflicts(tmp_path, "a.py", "foo", llm_arbitrate=True, llm_command=stub_command)
    assert found == []
