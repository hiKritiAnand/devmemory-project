"""Tests for HTML and CSS: no functions/classes/calls at all, just
custom_parser-based import extraction (<link>/<script> attributes, @import)."""

from devmemory import knowledge

HTML = '''<!DOCTYPE html>
<html>
<head>
  <link rel="stylesheet" href="./styles.css">
  <script src="./app.js"></script>
</head>
<body></body>
</html>
'''

CSS = '''
@import url("./base.css");
@import "./theme.css";

.button {
  color: red;
}
'''


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_html_extracts_link_and_script_src_as_imports(tmp_path):
    path = _write(tmp_path, "index.html", HTML)
    parsed = knowledge.parse_file(path, "html")

    assert "./styles.css" in parsed.imports
    assert "./app.js" in parsed.imports
    assert parsed.definitions == []


def test_css_extracts_both_import_forms(tmp_path):
    path = _write(tmp_path, "main.css", CSS)
    parsed = knowledge.parse_file(path, "css")

    assert "./base.css" in parsed.imports  # the url(...) form
    assert "./theme.css" in parsed.imports  # the bare string form
    assert parsed.definitions == []


def test_html_import_resolves_to_local_css_file_via_scan(git_repo):
    import subprocess
    _write(git_repo, "index.html", HTML)
    _write(git_repo, "styles.css", ".a { color: blue; }\n")
    _write(git_repo, "app.js", "console.log('hi');\n")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "web files"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    result = knowledge.inspect(git_repo, file="index.html")
    targets = {e["file"] for e in result["outgoing"] if e["edge_kind"] == "imports"}
    assert "styles.css" in targets
    assert "app.js" in targets
