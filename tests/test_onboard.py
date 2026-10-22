import subprocess

from devmemory import knowledge, onboard, storage


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit_all(root, message="commit"):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


def test_classify_file_recognizes_readme(tmp_path):
    path = _write(tmp_path, "README.md", "# Hello")
    assert onboard.classify_file(tmp_path, path) == {"kind": "readme", "scope_path": None}


def test_classify_file_recognizes_nested_agents_md_with_directory_scope(tmp_path):
    path = _write(tmp_path, "services/backend/AGENTS.md", "rules")
    assert onboard.classify_file(tmp_path, path) == {"kind": "agents_md", "scope_path": "services/backend"}


def test_classify_file_recognizes_claude_md(tmp_path):
    path = _write(tmp_path, "CLAUDE.md", "rules")
    assert onboard.classify_file(tmp_path, path) == {"kind": "claude_md", "scope_path": None}


def test_classify_file_recognizes_nested_cursor_rule_scoped_to_parent_dir(tmp_path):
    path = _write(tmp_path, "services/backend/.cursor/rules/devmemory.mdc", "rules")
    result = onboard.classify_file(tmp_path, path)
    assert result == {"kind": "cursor_rule", "scope_path": "services/backend"}


def test_classify_file_recognizes_root_cursor_rule_as_project_wide(tmp_path):
    path = _write(tmp_path, ".cursor/rules/devmemory.mdc", "rules")
    assert onboard.classify_file(tmp_path, path) == {"kind": "cursor_rule", "scope_path": None}


def test_classify_file_recognizes_copilot_instructions(tmp_path):
    path = _write(tmp_path, ".github/copilot-instructions.md", "rules")
    assert onboard.classify_file(tmp_path, path) == {"kind": "copilot_instructions", "scope_path": None}


def test_classify_file_recognizes_adr_directory(tmp_path):
    path = _write(tmp_path, "docs/adr/0001-use-sqlite.md", "decision")
    result = onboard.classify_file(tmp_path, path)
    assert result["kind"] == "adr"


def test_classify_file_recognizes_openapi_spec(tmp_path):
    path = _write(tmp_path, "api/openapi.yaml", "openapi: 3.0.0")
    assert onboard.classify_file(tmp_path, path) == {"kind": "openapi_spec", "scope_path": "api"}


def test_classify_file_recognizes_migration_directory(tmp_path):
    path = _write(tmp_path, "db/migrations/0001_init.sql", "CREATE TABLE x (id INT);")
    result = onboard.classify_file(tmp_path, path)
    assert result["kind"] == "db_migration"


def test_classify_file_returns_none_for_unrecognized_file(tmp_path):
    path = _write(tmp_path, "src/foo.py", "x = 1")
    assert onboard.classify_file(tmp_path, path) is None


def test_onboard_indexes_recognized_sources(tmp_path):
    _write(tmp_path, "README.md", "This project does X.")
    _write(tmp_path, "CLAUDE.md", "Always run tests before committing.")
    _write(tmp_path, "src/unrelated.py", "x = 1")

    report = onboard.onboard(tmp_path)

    assert report.indexed == 2
    assert report.failed == 0
    records = storage.query_context_records(tmp_path)
    assert {r["source_path"] for r in records} == {"README.md", "CLAUDE.md"}


def test_onboard_migration_file_content_is_summarized_not_full_sql(tmp_path):
    _write(tmp_path, "db/migrations/0001_init.sql", "-- revision 0001\nCREATE TABLE users (id INTEGER);\n" * 50)

    onboard.onboard(tmp_path)

    record = storage.query_context_records(tmp_path, kind="db_migration")[0]
    assert "0001_init.sql" in record["content"]
    assert len(record["content"]) < 200  # nowhere near the full repeated-50x SQL text


def test_onboard_is_idempotent_on_unchanged_repo(tmp_path):
    _write(tmp_path, "README.md", "This project does X.")

    first = onboard.onboard(tmp_path)
    assert first.indexed == 1

    second = onboard.onboard(tmp_path)
    assert second.indexed == 0
    assert second.skipped_unchanged == 1

    records = storage.query_context_records(tmp_path)
    assert len(records) == 1  # no duplicate row


def test_onboard_reindexes_a_changed_source_without_duplicating(tmp_path):
    _write(tmp_path, "README.md", "version 1")
    onboard.onboard(tmp_path)

    _write(tmp_path, "README.md", "version 2, substantially rewritten")
    report = onboard.onboard(tmp_path)

    assert report.indexed == 1
    records = storage.query_context_records(tmp_path)
    assert len(records) == 1
    assert records[0]["content"] == "version 2, substantially rewritten"


def test_onboard_dry_run_writes_nothing(tmp_path):
    _write(tmp_path, "README.md", "content")

    report = onboard.onboard(tmp_path, dry_run=True)

    assert report.indexed == 1
    assert storage.query_context_records(tmp_path) == []


def test_link_to_nodes_links_on_exact_unique_file_path_match(git_repo):
    _write(git_repo, "src/widget.py", "def build_widget():\n    return 1\n")
    _write(git_repo, "README.md", "See src/widget.py for the implementation.")
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    onboard.onboard(git_repo)

    record_id = storage.query_context_records(git_repo, kind="readme")[0]["id"]
    links = storage.get_decision_links(git_repo, dst_kind="context_record", dst_ref=str(record_id))
    assert len(links) >= 1
    assert all(link["kind"] == "DOCUMENTED_BY" for link in links)


def test_link_to_nodes_links_on_globally_unique_symbol_name(git_repo):
    _write(git_repo, "src/widget.py", "def build_super_unique_widget_name():\n    return 1\n")
    _write(git_repo, "README.md", "Call build_super_unique_widget_name() to construct it.")
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    onboard.onboard(git_repo)
    linked_count = onboard.link_to_nodes(git_repo)  # idempotent re-run: 0 new, already linked by onboard()

    assert linked_count == 0
    record_id = storage.query_context_records(git_repo, kind="readme")[0]["id"]
    links = storage.get_decision_links(git_repo, dst_kind="context_record", dst_ref=str(record_id))
    assert len(links) >= 1


def test_link_to_nodes_never_links_an_ambiguous_symbol_name(git_repo):
    _write(git_repo, "a.py", "def run():\n    return 1\n")
    _write(git_repo, "b.py", "def run():\n    return 2\n")
    _write(git_repo, "README.md", "Call run() to start.")
    _commit_all(git_repo)

    knowledge.scan(git_repo)
    onboard.onboard(git_repo)

    record_id = storage.query_context_records(git_repo, kind="readme")[0]["id"]
    links = storage.get_decision_links(git_repo, dst_kind="context_record", dst_ref=str(record_id))
    # "run" is defined in two files -- never ambiguously linked to either.
    assert links == []


def test_link_to_nodes_is_idempotent(git_repo):
    _write(git_repo, "src/widget.py", "def build_widget():\n    return 1\n")
    _write(git_repo, "README.md", "See src/widget.py.")
    _commit_all(git_repo)
    knowledge.scan(git_repo)
    storage.upsert_context_record(
        git_repo, kind="readme", source_path="README.md", content="See src/widget.py.",
        content_hash="irrelevant-for-this-test",
    )

    first = onboard.link_to_nodes(git_repo)
    second = onboard.link_to_nodes(git_repo)

    assert first >= 1
    assert second == 0  # already-linked pairs are never relinked


def test_onboard_malformed_binary_file_is_skipped_not_crashed(tmp_path):
    _write(tmp_path, "README.md", "fine")
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "0001-binary.md").write_bytes(b"\xff\xfe\x00\x01binary garbage\x80\x81")

    report = onboard.onboard(tmp_path)

    assert report.failed == 0
    assert report.skipped_unrecognized == 1
    assert report.indexed == 1  # just the README
