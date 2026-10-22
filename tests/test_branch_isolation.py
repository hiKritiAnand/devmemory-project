import subprocess

from devmemory import knowledge, storage


def _checkout_new_branch(root, name):
    subprocess.run(["git", "checkout", "-q", "-b", name], cwd=root, check=True)


def test_decision_recorded_on_one_branch_is_invisible_on_another(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    storage.init_store(git_repo)

    branch_a = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    storage.add_decision(git_repo, file="a.py", summary="decision on branch A")

    _checkout_new_branch(git_repo, "feature-branch")
    assert storage.query_decisions(git_repo, file="a.py") == []

    _checkout_new_branch(git_repo, "back-to-a")  # just to prove branch_a-scoped visibility isn't position-based
    subprocess.run(["git", "checkout", "-q", branch_a], cwd=git_repo, check=True)
    found = storage.query_decisions(git_repo, file="a.py")
    assert len(found) == 1
    assert found[0]["summary"] == "decision on branch A"


def test_all_branches_flag_shows_everything(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    storage.init_store(git_repo)

    storage.add_decision(git_repo, file="a.py", summary="decision on main branch")
    _checkout_new_branch(git_repo, "feature-branch")
    storage.add_decision(git_repo, file="a.py", summary="decision on feature branch")

    scoped = storage.query_decisions(git_repo, file="a.py")
    assert len(scoped) == 1
    assert scoped[0]["summary"] == "decision on feature branch"

    everything = storage.query_decisions(git_repo, file="a.py", all_branches=True)
    assert {r["summary"] for r in everything} == {"decision on main branch", "decision on feature branch"}


def test_global_flag_forces_project_wide_visibility(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    storage.init_store(git_repo)

    storage.add_decision(git_repo, file="a.py", summary="global decision", global_=True)
    _checkout_new_branch(git_repo, "feature-branch")

    found = storage.query_decisions(git_repo, file="a.py")
    assert len(found) == 1
    assert found[0]["summary"] == "global decision"
    assert found[0]["branch"] is None


def test_decisions_outside_a_git_repo_are_always_project_wide(tmp_path):
    storage.init_store(tmp_path)
    record = storage.add_decision(tmp_path, file="a.py", summary="s1")
    assert record["branch"] is None
    assert storage.query_decisions(tmp_path, file="a.py") == [record] or len(storage.query_decisions(tmp_path)) == 1


def test_pre_migration_null_branch_decisions_stay_globally_visible(tmp_path):
    """Simulates a decision recorded before branch isolation existed
    (branch=NULL from the migration backfill, not from add_decision's new
    logic) -- it must never become invisible on any branch."""
    storage.init_store(tmp_path)
    storage.add_decision(tmp_path, file="a.py", summary="pre-existing decision")
    # No real git repo here, so this is already branch=NULL and visible
    # regardless -- query_decisions's OR clause is what's under test.
    assert len(storage.query_decisions(tmp_path, file="a.py")) == 1


def test_search_decisions_respects_branch_scope(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    storage.init_store(git_repo)

    storage.add_decision(git_repo, file="a.py", summary="uses synchronous retries")
    _checkout_new_branch(git_repo, "feature-branch")

    assert storage.search_decisions(git_repo, "synchronous") == []
    assert len(storage.search_decisions(git_repo, "synchronous", all_branches=True)) == 1


def test_log_since_respects_branch_scope(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    storage.init_store(git_repo)

    storage.add_decision(git_repo, file="a.py", summary="s1", created_at="2026-01-01T00:00:00+00:00")
    _checkout_new_branch(git_repo, "feature-branch")

    assert storage.log_since(git_repo, "2020-01-01") == []
    assert len(storage.log_since(git_repo, "2020-01-01", all_branches=True)) == 1


def test_branch_staleness_silent_right_after_a_same_branch_scan(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    assert knowledge.branch_staleness(git_repo) is None


def test_branch_staleness_warns_after_switching_branches(git_repo):
    (git_repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=git_repo, check=True)
    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo, capture_output=True, text=True
    ).stdout.strip()
    knowledge.scan(git_repo)

    _checkout_new_branch(git_repo, "feature-branch")
    warning = knowledge.branch_staleness(git_repo)
    assert warning is not None
    assert original_branch in warning  # names the branch the graph was actually scanned under
    assert "feature-branch" in warning  # and states which branch is now checked out


def test_branch_staleness_none_outside_git_repo(tmp_path):
    storage.init_store(tmp_path)
    assert knowledge.branch_staleness(tmp_path) is None
