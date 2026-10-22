import subprocess

from devmemory import knowledge


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit_all(root, message="commit"):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def test_unchanged_file_adds_zero_node_history_rows(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    knowledge.scan(git_repo)  # nothing changed -- file is skipped entirely

    assert knowledge.node_history(git_repo, "a.py", "foo") == []


def test_changed_docstring_produces_exactly_one_history_row(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    _write(git_repo, "a.py", "def foo():\n    '''now documented'''\n    return 1\n")
    _commit_all(git_repo, "document foo")
    knowledge.scan(git_repo)

    history = knowledge.node_history(git_repo, "a.py", "foo")
    assert len(history) == 1


def test_unrelated_sibling_function_change_does_not_history_an_untouched_symbol(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 1\n\ndef bar():\n    return 2\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    # Only bar's body changes; foo is untouched even though the whole file
    # gets reparsed (it has to be, since the file's content hash changed).
    _write(git_repo, "a.py", "def foo():\n    return 1\n\ndef bar():\n    return 999\n")
    _commit_all(git_repo, "change bar")
    knowledge.scan(git_repo)

    assert knowledge.node_history(git_repo, "a.py", "foo") == []
    assert len(knowledge.node_history(git_repo, "a.py", "bar")) == 1


def test_in_file_rename_with_unchanged_body_produces_a_high_confidence_candidate(git_repo):
    _write(git_repo, "a.py", "def old_name():\n    return 42\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    _write(git_repo, "a.py", "def new_name():\n    return 42\n")
    _commit_all(git_repo, "rename")
    knowledge.scan(git_repo)

    candidates = knowledge.rename_candidates(git_repo)
    assert len(candidates) == 1
    assert candidates[0]["old_node_file"] == "a.py"
    assert candidates[0]["old_node_symbol"] == "old_name"
    assert candidates[0]["confidence"] == "high"
    assert candidates[0]["resolved"] == 0

    # Old and new are never merged into one node/identity.
    old_history = knowledge.node_history(git_repo, "a.py", "old_name")
    assert len(old_history) == 1
    assert old_history[0]["body_hash"] is not None


def test_cross_file_move_produces_a_low_confidence_candidate(git_repo):
    _write(git_repo, "a.py", "def helper():\n    return 42\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    (git_repo / "a.py").unlink()
    _write(git_repo, "b.py", "def helper():\n    return 42\n")
    _commit_all(git_repo, "move helper to b.py")
    knowledge.scan(git_repo)

    candidates = knowledge.rename_candidates(git_repo)
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "low"
    assert candidates[0]["old_node_file"] == "a.py"


def test_unrelated_duplicate_bodies_are_never_merged_only_surfaced_low_confidence(git_repo):
    _write(git_repo, "a.py", "def one():\n    pass\n")
    _write(git_repo, "b.py", "def two():\n    pass\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    # `one` is removed outright (not renamed within a.py); `two` in b.py
    # (unrelated, identical trivial body "pass") is reparsed too -- since
    # b.py's own content changes (a sibling function added) even though
    # `two`'s own body is untouched -- producing a coincidental body_hash
    # match across files. Documented as a known, accepted noise source: a
    # low-confidence candidate, never auto-merged, never promoted to high.
    (git_repo / "a.py").write_text("")
    _write(git_repo, "b.py", "def two():\n    pass\n\ndef three():\n    return 1\n")
    _commit_all(git_repo, "remove one; unrelated change to b.py")
    knowledge.scan(git_repo)

    candidates = knowledge.rename_candidates(git_repo)
    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "low"
    assert candidates[0]["old_node_file"] == "a.py"
    assert candidates[0]["old_node_symbol"] == "one"
    # Nodes list still shows `two` and `three` as distinct, separate symbols -- never merged.
    stats = knowledge.stats(git_repo)
    assert stats["nodes"]["function"] == 2


def test_mark_rename_candidate_resolved(git_repo):
    _write(git_repo, "a.py", "def old_name():\n    return 1\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)
    _write(git_repo, "a.py", "def new_name():\n    return 1\n")
    _commit_all(git_repo, "rename")
    knowledge.scan(git_repo)

    candidate_id = knowledge.rename_candidates(git_repo)[0]["id"]
    knowledge.mark_rename_candidate_resolved(git_repo, candidate_id)

    assert knowledge.rename_candidates(git_repo, resolved=True)[0]["id"] == candidate_id
    assert knowledge.rename_candidates(git_repo, resolved=False) == []


def test_deleted_symbol_with_no_body_match_produces_no_rename_candidate(git_repo):
    _write(git_repo, "a.py", "def foo():\n    return 'something unique and never reused'\n")
    _commit_all(git_repo)
    knowledge.scan(git_repo)

    (git_repo / "a.py").unlink()
    _commit_all(git_repo, "remove foo")
    knowledge.scan(git_repo)

    assert knowledge.rename_candidates(git_repo) == []
    history = knowledge.node_history(git_repo, "a.py", "foo")
    assert len(history) == 1
