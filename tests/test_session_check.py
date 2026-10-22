import subprocess

from devmemory import session_check, storage


def test_get_dirty_files_lists_modified_and_untracked_excluding_devmemory(git_repo):
    (git_repo / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=git_repo, check=True)

    (git_repo / "a.py").write_text("a = 2\n")
    (git_repo / "b.py").write_text("b = 1\n")
    (git_repo / ".devmemory").mkdir()
    (git_repo / ".devmemory" / "memory.db").write_text("")

    assert session_check.get_dirty_files(git_repo) == ["a.py", "b.py"]


def test_get_dirty_files_returns_empty_outside_git_repo(tmp_path):
    assert session_check.get_dirty_files(tmp_path) == []


def test_get_dirty_files_handles_renames(git_repo):
    (git_repo / "old.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "old.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add old"], cwd=git_repo, check=True)

    subprocess.run(["git", "mv", "old.py", "new.py"], cwd=git_repo, check=True)

    assert session_check.get_dirty_files(git_repo) == ["new.py"]


def test_check_session_allows_silently_when_stop_hook_already_active(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=True) is None


def test_check_session_allows_silently_when_no_dirty_files(git_repo):
    storage.init_store(git_repo)

    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is None


def test_check_session_allows_silently_when_store_not_initialized(git_repo):
    (git_repo / "a.py").write_text("a = 1\n")

    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is None


def test_check_session_blocks_on_first_unresolved_dirty_set(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    reason = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)

    assert reason is not None
    assert "a.py" in reason


def test_check_session_does_not_renag_same_unresolved_set_twice(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    first = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)
    second = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)

    assert first is not None
    assert second is None


def test_check_session_renags_when_dirty_set_changes(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")
    first = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)
    assert first is not None

    (git_repo / "b.py").write_text("b = 1\n")
    second = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)

    assert second is not None
    assert "b.py" in second


def test_check_session_allows_once_matching_record_recorded(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    first = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)
    assert first is not None

    storage.add_decision(git_repo, file="a.py", summary="did something")

    second = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)
    assert second is None


def test_check_session_resets_nudge_after_resolution_so_future_unrelated_changes_can_nudge_again(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is not None
    storage.add_decision(git_repo, file="a.py", summary="did something")
    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is None

    (git_repo / "b.py").write_text("b = 1\n")
    third = session_check.check_session(git_repo, session_id=None, stop_hook_active=False)

    assert third is not None
    assert "b.py" in third


def test_check_session_clears_state_when_dirty_files_become_empty(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")
    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is not None

    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=git_repo, check=True)

    assert session_check.check_session(git_repo, session_id=None, stop_hook_active=False) is None
    assert not (git_repo / ".devmemory" / "stop_nudge_state.json").exists()


def test_handle_stdin_payload_returns_block_dict_shape(git_repo):
    storage.init_store(git_repo)
    (git_repo / "a.py").write_text("a = 1\n")

    result = session_check.handle_stdin_payload(git_repo, {"session_id": "abc", "stop_hook_active": False})

    assert result == {"decision": "block", "reason": result["reason"]}
    assert "a.py" in result["reason"]


def test_handle_stdin_payload_returns_none_when_allowed(git_repo):
    storage.init_store(git_repo)

    assert session_check.handle_stdin_payload(git_repo, {"stop_hook_active": False}) is None


def test_hash_paths_is_order_independent():
    assert session_check._hash_paths(["a.py", "b.py"]) == session_check._hash_paths(["b.py", "a.py"])
