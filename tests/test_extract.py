import json
import subprocess
import sys

import pytest

from devmemory import extract, storage


def _commit(root, path, content, message):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text(content)
    subprocess.run(["git", "add", path], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def _write_stub(tmp_path, body, name="stub.py"):
    stub = tmp_path / name
    stub.write_text(body)
    return [sys.executable, str(stub)]


def test_get_changed_paths_returns_files_from_commit(git_repo):
    (git_repo / "a.py").write_text("a = 1\n")
    (git_repo / "b.py").write_text("b = 2\n")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a and b"], cwd=git_repo, check=True)

    assert set(extract.get_changed_paths(git_repo, "HEAD")) == {"a.py", "b.py"}


def test_filter_self_reference_paths_excludes_devmemory_dir():
    paths = [".devmemory/records/000001.md", ".devmemory", "a.py", ".devmemoryx.py"]
    assert extract.filter_self_reference_paths(paths) == ["a.py", ".devmemoryx.py"]


def test_get_commit_diff_for_paths_includes_only_given_paths(git_repo):
    _commit(git_repo, "a.py", "a = 1\n", "add a")
    _commit(git_repo, "b.py", "b = 2\n", "add b")

    diff = extract.get_commit_diff_for_paths(git_repo, "HEAD", ["b.py"])
    assert "b.py" in diff
    assert "a.py" not in diff


def test_truncate_diff_under_limit_returns_unchanged():
    diff, truncated = extract.truncate_diff("short diff", max_chars=100)
    assert diff == "short diff"
    assert truncated is False


def test_truncate_diff_over_limit_truncates_and_flags():
    diff, truncated = extract.truncate_diff("x" * 20000, max_chars=100)
    assert len(diff) == 100
    assert truncated is True


def test_build_prompt_includes_truncation_note_when_truncated():
    truncated_prompt = extract.build_prompt("msg", "diff", truncated=True)
    plain_prompt = extract.build_prompt("msg", "diff", truncated=False)
    assert "truncated" in truncated_prompt
    assert "truncated" not in plain_prompt


def test_parse_llm_output_handles_claude_cli_envelope():
    structured = {"worth_recording": True, "decisions": []}
    envelope = json.dumps({"result": "some text", "structured_output": structured})
    assert extract.parse_llm_output(envelope) == structured


def test_parse_llm_output_handles_bare_json_object():
    bare = {"worth_recording": False, "decisions": []}
    assert extract.parse_llm_output(json.dumps(bare)) == bare


def test_parse_llm_output_raises_on_empty_output():
    with pytest.raises(ValueError):
        extract.parse_llm_output("   ")


def test_run_llm_uses_injected_llm_command_and_feeds_prompt_on_stdin(tmp_path):
    captured = tmp_path / "captured.txt"
    stub_body = (
        "import sys, json\n"
        "data = sys.stdin.read()\n"
        f"open({str(captured)!r}, 'w').write(data)\n"
        "print(json.dumps({'worth_recording': False, 'decisions': []}))\n"
    )
    command = _write_stub(tmp_path, stub_body)

    result = extract.run_llm("the prompt text", llm_command=command)

    assert captured.read_text() == "the prompt text"
    assert result == {"worth_recording": False, "decisions": []}


def test_run_llm_raises_on_nonzero_exit(tmp_path):
    stub_body = "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n"
    command = _write_stub(tmp_path, stub_body)

    with pytest.raises(RuntimeError, match="failed"):
        extract.run_llm("prompt", llm_command=command)


def test_run_llm_raises_on_missing_command():
    with pytest.raises(RuntimeError, match="not found"):
        extract.run_llm("prompt", llm_command=["/no/such/binary-devmemory-test"])


def test_extract_and_record_skips_when_only_self_reference_paths_changed(git_repo, monkeypatch):
    storage.init_store(git_repo)
    (git_repo / ".devmemory" / "note.txt").write_text("hi\n")
    subprocess.run(["git", "add", "-f", ".devmemory/note.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "touch devmemory dir"], cwd=git_repo, check=True)

    def crash_if_called(*args, **kwargs):
        raise AssertionError("LLM should not be invoked when only self-reference paths changed")

    monkeypatch.setattr(extract, "run_llm", crash_if_called)

    assert extract.extract_and_record(git_repo, commit_ref="HEAD") == []


def test_extract_and_record_skips_and_does_not_record_when_llm_says_not_worth_recording(git_repo, tmp_path):
    _commit(git_repo, "a.py", "a = 1\n", "add a")
    command = _write_stub(
        tmp_path,
        "import json\nprint(json.dumps({'worth_recording': False, 'decisions': []}))\n",
    )

    records = extract.extract_and_record(git_repo, llm_command=command)

    assert records == []
    assert storage.query_decisions(git_repo) == []


def test_extract_and_record_adds_decision_rows_from_llm_output(git_repo, tmp_path):
    _commit(git_repo, "a.py", "def foo():\n    return 1\n", "add foo")
    command = _write_stub(
        tmp_path,
        "import json\n"
        "print(json.dumps({'worth_recording': True, 'decisions': ["
        "{'file': 'a.py', 'symbol': 'foo', 'summary': 'added foo', "
        "'why': 'needed', 'rejected': None, 'tags': 'feature'}]}))\n",
    )

    records = extract.extract_and_record(git_repo, llm_command=command)

    assert len(records) == 1
    assert records[0]["file"] == "a.py"
    sha = extract.get_commit_sha(git_repo, "HEAD")
    assert records[0]["session_ref"] == f"commit:{sha}"
    assert len(storage.query_decisions(git_repo, file="a.py")) == 1


def test_extract_and_record_handles_multiple_decisions_in_one_commit(git_repo, tmp_path):
    (git_repo / "a.py").write_text("a = 1\n")
    (git_repo / "b.py").write_text("b = 2\n")
    subprocess.run(["git", "add", "a.py", "b.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a and b"], cwd=git_repo, check=True)

    command = _write_stub(
        tmp_path,
        "import json\n"
        "print(json.dumps({'worth_recording': True, 'decisions': ["
        "{'file': 'a.py', 'summary': 'added a', 'symbol': None, 'why': None, 'rejected': None, 'tags': None},"
        "{'file': 'b.py', 'summary': 'added b', 'symbol': None, 'why': None, 'rejected': None, 'tags': None}"
        "]}))\n",
    )

    records = extract.extract_and_record(git_repo, llm_command=command)

    assert {r["file"] for r in records} == {"a.py", "b.py"}


def test_extract_and_record_redacts_secrets_from_diff_before_llm_sees_it(git_repo, tmp_path, capsys):
    _commit(git_repo, "config.py", "AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n", "add config")
    captured = tmp_path / "captured_prompt.txt"
    command = _write_stub(
        tmp_path,
        "import sys, json\n"
        f"open({str(captured)!r}, 'w').write(sys.stdin.read())\n"
        "print(json.dumps({'worth_recording': False, 'decisions': []}))\n",
    )

    extract.extract_and_record(git_repo, llm_command=command)

    prompt_sent = captured.read_text()
    assert "AKIAABCDEFGHIJKLMNOP" not in prompt_sent
    assert "[REDACTED:aws_access_key_id]" in prompt_sent
    assert "redacted 1 likely secret" in capsys.readouterr().err


def test_extract_and_record_auto_initializes_store_if_missing(git_repo, tmp_path):
    _commit(git_repo, "a.py", "a = 1\n", "add a")
    command = _write_stub(
        tmp_path,
        "import json\nprint(json.dumps({'worth_recording': False, 'decisions': []}))\n",
    )

    assert not storage.get_db_path(git_repo).exists()
    extract.extract_and_record(git_repo, llm_command=command)
    assert storage.get_db_path(git_repo).exists()
