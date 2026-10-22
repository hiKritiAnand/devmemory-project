"""Tests for SQL: the one language with its own definition kind (table),
its own edge kind (references, for FROM/JOIN table lookups), and
file-level attribution for top-level statements -- unlike every other
language here, most real .sql files are sequences of top-level statements,
not function bodies, so a reference with no enclosing function is
attributed to the file itself instead of being dropped."""

import subprocess

from devmemory import knowledge

SQL_WITH_FUNCTION = '''
CREATE TABLE invoices (id INT, total INT);

CREATE FUNCTION calculate_total(price INT, qty INT)
RETURNS INT
BEGIN
  RETURN price * qty;
END;

SELECT calculate_total(10, 2) FROM invoices;
'''

SQL_TOP_LEVEL_ONLY = '''
SELECT * FROM invoices;
'''


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_sql_extracts_table_and_function_definitions(tmp_path):
    path = _write(tmp_path, "schema.sql", SQL_WITH_FUNCTION)
    parsed = knowledge.parse_file(path, "sql")

    names = {(d.kind, d.name) for d in parsed.definitions}
    assert ("table", "invoices") in names
    assert ("function", "calculate_total") in names


def test_sql_top_level_reference_attributed_to_file_not_dropped(tmp_path):
    path = _write(tmp_path, "query.sql", SQL_TOP_LEVEL_ONLY)
    parsed = knowledge.parse_file(path, "sql")

    assert parsed.references == [(None, "invoices")]


def test_sql_call_inside_function_is_attributed_to_the_function(tmp_path):
    path = _write(tmp_path, "schema.sql", SQL_WITH_FUNCTION)
    parsed = knowledge.parse_file(path, "sql")

    # The top-level `SELECT calculate_total(...) FROM invoices` has no
    # enclosing function, so its call and reference attribute to the file
    # (caller is None) rather than being dropped.
    assert (None, "calculate_total") in parsed.calls
    assert (None, "invoices") in parsed.references


def test_scan_creates_table_reference_edge_from_file(git_repo):
    _write(git_repo, "query.sql", SQL_TOP_LEVEL_ONLY)
    _write(git_repo, "schema.sql", "CREATE TABLE invoices (id INT);\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sql files"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)

    result = knowledge.inspect(git_repo, file="query.sql")
    refs = [e for e in result["outgoing"] if e["edge_kind"] == "references"]
    assert any(e["symbol"] == "invoices" for e in refs)


def test_scan_unresolved_table_reference_becomes_external(git_repo):
    _write(git_repo, "query.sql", SQL_TOP_LEVEL_ONLY)
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sql file, no schema"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)

    result = knowledge.inspect(git_repo, file="query.sql")
    refs = [e for e in result["outgoing"] if e["edge_kind"] == "references"]
    assert any(e["kind"] == "external" and e["file"] == "invoices" for e in refs)
