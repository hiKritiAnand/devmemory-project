import io
import json
import shlex
import subprocess
import sys

import pytest

from devmemory.cli import main


def run(capsys, argv):
    main(argv)
    return capsys.readouterr().out


def test_version_flag_prints_nonempty_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "devmemory" in out
    assert len(out.strip()) > len("devmemory ")


def test_version_flag_matches_importlib_metadata(capsys):
    import importlib.metadata
    from devmemory.cli import get_version

    assert get_version() == importlib.metadata.version("devmemory")


def test_status_command_on_uninitialized_store(tmp_path, capsys):
    out = run(capsys, ["--path", str(tmp_path), "status"])
    assert "not initialized" in out


def test_status_command_on_initialized_store(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "status"])
    assert "Decisions recorded: 1" in out
    assert "Schema version:" in out
    assert "not yet scanned" in out


def test_status_command_after_knowledge_scan(git_repo, capsys):
    (git_repo / "a.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "init"])
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    out = run(capsys, ["--path", str(git_repo), "status"])
    assert "node(s)" in out
    assert "Git branch:" in out


def test_doctor_command_reports_uninitialized_as_needing_attention(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--path", str(tmp_path), "doctor"])
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "not initialized" in out


def test_doctor_command_passes_when_fully_set_up(git_repo, capsys):
    (git_repo / "a.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "init"])
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    out = run(capsys, ["--path", str(git_repo), "doctor"])
    assert "All checks passed." in out


def test_explain_command_finds_matching_decision(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "uses synchronous retries"])

    out = run(capsys, ["--path", str(tmp_path), "explain", "synchronous retries"])
    assert "keyword search" in out
    assert "synchronous retries" in out


def test_explain_command_with_no_matches(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    out = run(capsys, ["--path", str(tmp_path), "explain", "nonexistent topic xyz"])
    assert "Nothing found" in out


def test_memory_query_command_finds_matching_decision(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "uses exponential backoff"])

    out = run(capsys, ["--path", str(tmp_path), "memory", "query", "exponential backoff"])
    assert "keyword search" in out
    assert "exponential backoff" in out


def test_memory_query_command_with_no_matches(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    out = run(capsys, ["--path", str(tmp_path), "memory", "query", "nonexistent topic xyz"])
    assert "No matching decisions found." in out


def test_init_command(tmp_path, capsys):
    out = run(capsys, ["--path", str(tmp_path), "init"])

    assert "Initialized" in out
    assert (tmp_path / ".devmemory" / "memory.db").exists()


def test_record_and_query_roundtrip(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    out = run(
        capsys,
        [
            "--path", str(tmp_path), "record",
            "--file", "src/foo.py",
            "--symbol", "do_thing",
            "--summary", "Did the thing",
            "--why", "Needed speed",
            "--rejected", "Kept the old approach",
            "--tags", "perf,refactor",
            "--author", "kriti",
        ],
    )
    assert "Recorded decision #1" in out

    out = run(capsys, ["--path", str(tmp_path), "query", "--file", "src/foo.py"])
    assert "Did the thing" in out
    assert "do_thing" in out


def test_record_with_status_and_decisional_fields(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    run(
        capsys,
        [
            "--path", str(tmp_path), "record",
            "--file", "a.py", "--summary", "Proposed new caching layer",
            "--status", "proposed",
            "--alternatives", "In-memory dict cache",
            "--consequences", "Adds a new dependency",
            "--scope", "src/cache/",
            "--owner", "kriti",
        ],
    )

    out = run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py"])
    assert "(proposed)" in out


def test_record_rejects_invalid_status(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    with pytest.raises(SystemExit):
        main(["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1", "--status", "bogus"])


def test_record_with_supersedes_marks_old_decision_superseded(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "old approach"])

    out = run(
        capsys,
        [
            "--path", str(tmp_path), "record",
            "--file", "a.py", "--summary", "new approach", "--supersedes", "1",
        ],
    )
    assert "superseded by #2" in out

    query_out = run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py"])
    assert "(superseded, superseded by #2)" in query_out


def test_set_status_command_changes_status(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "set-status", "1", "deprecated"])
    assert "now deprecated" in out


def test_set_status_command_rejects_unknown_decision(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    with pytest.raises(SystemExit):
        main(["--path", str(tmp_path), "set-status", "999", "deprecated"])


def test_history_command_lists_events(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])
    run(capsys, ["--path", str(tmp_path), "set-status", "1", "deprecated"])

    out = run(capsys, ["--path", str(tmp_path), "history", "--decision-id", "1"])
    assert "created" in out
    assert "status_changed" in out


def test_history_command_with_no_history(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    out = run(capsys, ["--path", str(tmp_path), "history", "--decision-id", "999"])
    assert "No history recorded" in out


def test_record_warns_on_conflict_with_existing_active_decision(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(
        capsys,
        [
            "--path", str(tmp_path), "record", "--file", "a.py", "--symbol", "foo",
            "--summary", "Use synchronous retries", "--status", "accepted",
        ],
    )
    main(
        [
            "--path", str(tmp_path), "record", "--file", "a.py", "--symbol", "foo",
            "--summary", "Never use synchronous retries", "--status", "accepted",
        ]
    )
    captured = capsys.readouterr()
    assert "Recorded decision #2" in captured.out
    assert "WARNING: possible conflict" in captured.err


def test_conflicts_command_reports_no_conflicts(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "conflicts", "--file", "a.py"])
    assert "No conflicts detected." in out


def test_conflicts_command_reports_detected_conflicts(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(
        capsys,
        ["--path", str(tmp_path), "record", "--file", "a.py", "--symbol", "foo", "--summary", "Use retries"],
    )
    run(
        capsys,
        ["--path", str(tmp_path), "record", "--file", "a.py", "--symbol", "foo", "--summary", "Never use retries"],
    )

    out = run(capsys, ["--path", str(tmp_path), "conflicts", "--file", "a.py", "--symbol", "foo"])
    assert "keyword_negation" in out


def test_query_with_no_matches_reports_clearly(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    out = run(capsys, ["--path", str(tmp_path), "query", "--file", "nope.py"])
    assert "No decision records found." in out


def test_log_command(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "log", "--since", "2020-01-01"])
    assert "s1" in out


def test_log_command_with_no_matches(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])

    out = run(capsys, ["--path", str(tmp_path), "log", "--since", "2099-01-01"])
    assert "No decisions since" in out


def test_install_claude_writes_claude_md(tmp_path, capsys):
    out = run(capsys, ["--path", str(tmp_path), "install", "claude"])

    assert "claude" in out
    claude_md = tmp_path / "CLAUDE.md"
    assert claude_md.exists()
    assert "devmemory" in claude_md.read_text()


def test_install_cursor_writes_rules_file(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "install", "cursor"])

    rules = tmp_path / ".cursor" / "rules" / "devmemory.mdc"
    assert rules.exists()
    assert "devmemory" in rules.read_text()


def test_query_compile_flag_produces_budgeted_output(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s2"])

    out = run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py", "--compile"])
    assert "compiled:" in out
    assert "tokens (estimated)" in out


def test_query_without_compile_flag_is_unchanged_default_output(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    run(capsys, ["--path", str(tmp_path), "record", "--file", "a.py", "--summary", "s1"])

    out = run(capsys, ["--path", str(tmp_path), "query", "--file", "a.py"])
    assert "compiled:" not in out


def test_context_compile_flag_produces_budgeted_output(git_repo, capsys):
    (git_repo / "chain.py").write_text("def outer():\n    return middle()\n\ndef middle():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    out = run(
        capsys,
        [
            "--path", str(git_repo), "context", "--file", "chain.py", "--symbol", "middle",
            "--profile", "change_impact", "--compile",
        ],
    )
    assert "compiled:" in out


def test_profiles_command_lists_all_profiles(tmp_path, capsys):
    out = run(capsys, ["--path", str(tmp_path), "profiles"])
    assert "explanation" in out
    assert "change_impact" in out


def test_context_command_with_profile(git_repo, capsys):
    (git_repo / "chain.py").write_text(
        "def outer():\n    return middle()\n\ndef middle():\n    return 1\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    out = run(
        capsys,
        ["--path", str(git_repo), "context", "--file", "chain.py", "--symbol", "middle", "--profile", "change_impact"],
    )
    assert "Profile: change_impact" in out
    assert "outer" in out


def test_context_command_unknown_symbol(git_repo, capsys):
    (git_repo / "a.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    out = run(capsys, ["--path", str(git_repo), "context", "--file", "a.py", "--symbol", "nope"])
    assert "No node found" in out


def test_onboard_command_indexes_and_reports(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    (tmp_path / "README.md").write_text("This project does X.")

    out = run(capsys, ["--path", str(tmp_path), "onboard"])
    assert "Indexed 1 context source" in out
    assert "Linked" in out


def test_onboard_command_dry_run_writes_nothing(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    (tmp_path / "README.md").write_text("This project does X.")

    out = run(capsys, ["--path", str(tmp_path), "onboard", "--dry-run"])
    assert "Would index 1 context source" in out

    from devmemory import storage
    assert storage.query_context_records(tmp_path) == []


def test_top_level_scan_alias_matches_knowledge_scan(git_repo, capsys):
    (git_repo / "a.py").write_text("def foo():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "c"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "init"])

    out = run(capsys, ["--path", str(git_repo), "scan"])
    assert "Scanned" in out
    assert "Graph now has" in out


def test_knowledge_history_and_renames_commands(git_repo, capsys):
    (git_repo / "a.py").write_text("def old_name():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    (git_repo / "a.py").write_text("def new_name():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "rename"], cwd=git_repo, check=True)
    run(capsys, ["--path", str(git_repo), "knowledge", "scan"])

    history_out = run(
        capsys, ["--path", str(git_repo), "knowledge", "history", "--file", "a.py", "--symbol", "old_name"]
    )
    assert "function" in history_out

    renames_out = run(capsys, ["--path", str(git_repo), "knowledge", "renames"])
    assert "old_name" in renames_out
    assert "high confidence" in renames_out

    # Extract the candidate id from the printed line ("#<id> [...").
    candidate_id = renames_out.split("#", 1)[1].split(" ", 1)[0]
    out = run(capsys, ["--path", str(git_repo), "knowledge", "renames", "--mark-resolved", candidate_id])
    assert "resolved" in out

    resolved_out = run(capsys, ["--path", str(git_repo), "knowledge", "renames", "--resolved"])
    assert "old_name" in resolved_out


def test_knowledge_history_with_no_history(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    out = run(capsys, ["--path", str(tmp_path), "knowledge", "history", "--file", "a.py", "--symbol", "foo"])
    assert "No history recorded" in out


def test_knowledge_renames_with_no_candidates(tmp_path, capsys):
    run(capsys, ["--path", str(tmp_path), "init"])
    out = run(capsys, ["--path", str(tmp_path), "knowledge", "renames"])
    assert "No rename candidates." in out


def test_path_option_works_before_or_after_subcommand(tmp_path, capsys):
    out_before = run(capsys, ["--path", str(tmp_path), "init"])
    assert "Initialized" in out_before

    other = tmp_path / "other"
    other.mkdir()
    out_after = run(capsys, ["init", "--path", str(other)])
    assert "Initialized" in out_after
    assert (other / ".devmemory" / "memory.db").exists()


def test_install_git_writes_executable_hook(git_repo, capsys):
    out = run(capsys, ["--path", str(git_repo), "install", "git"])

    assert "git" in out
    hook_path = git_repo / ".git" / "hooks" / "post-commit"
    assert hook_path.exists()
    assert hook_path.stat().st_mode & 0o100


def _commit_file(root, path, content):
    (root / path).write_text(content)
    subprocess.run(["git", "add", path], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {path}"], cwd=root, check=True)


def test_extract_command_records_decision_via_injected_llm_command(git_repo, tmp_path, capsys):
    run(capsys, ["--path", str(git_repo), "init"])
    _commit_file(git_repo, "a.py", "def foo():\n    return 1\n")

    stub = tmp_path / "stub.py"
    stub.write_text(
        "import json\n"
        "print(json.dumps({'worth_recording': True, 'decisions': ["
        "{'file': 'a.py', 'summary': 'added foo', 'symbol': None, 'why': None, 'rejected': None, 'tags': None}"
        "]}))\n"
    )
    llm_command = shlex.join([sys.executable, str(stub)])

    out = run(capsys, ["--path", str(git_repo), "extract", "--llm-command", llm_command])

    assert "Recorded decision #1" in out


def test_extract_command_reports_no_decision_worthy_changes(git_repo, tmp_path, capsys):
    run(capsys, ["--path", str(git_repo), "init"])
    _commit_file(git_repo, "a.py", "a = 1\n")

    stub = tmp_path / "stub.py"
    stub.write_text("import json\nprint(json.dumps({'worth_recording': False, 'decisions': []}))\n")
    llm_command = shlex.join([sys.executable, str(stub)])

    out = run(capsys, ["--path", str(git_repo), "extract", "--llm-command", llm_command])

    assert "No decision-worthy changes found" in out


def test_extract_command_fails_gracefully_on_llm_error(git_repo, capsys):
    run(capsys, ["--path", str(git_repo), "init"])
    _commit_file(git_repo, "a.py", "a = 1\n")

    with pytest.raises(SystemExit):
        main(["--path", str(git_repo), "extract", "--llm-command", "/no/such/binary-devmemory-test"])


def test_check_session_command_blocks_and_prints_json(git_repo, monkeypatch, capsys):
    run(capsys, ["--path", str(git_repo), "init"])
    (git_repo / "a.py").write_text("a = 1\n")

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": False})))
    main(["--path", str(git_repo), "check-session"])

    data = json.loads(capsys.readouterr().out)
    assert data["decision"] == "block"
    assert "a.py" in data["reason"]


def test_check_session_command_silent_when_stop_hook_active(git_repo, monkeypatch, capsys):
    run(capsys, ["--path", str(git_repo), "init"])
    (git_repo / "a.py").write_text("a = 1\n")

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": True})))
    main(["--path", str(git_repo), "check-session"])

    assert capsys.readouterr().out == ""
