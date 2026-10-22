"""Tests for EXTENDS/IMPLEMENTS extraction (Phase 5). Each snippet's expected
tree shape was verified against real tree-sitter output before being encoded
in knowledge.py -- not guessed. See each language's `_x_inherit` docstring
in knowledge.py for the verified field names this relies on.
"""

from devmemory import knowledge


def _write(root, rel_path, content):
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_python_single_base_class(tmp_path):
    path = _write(tmp_path, "a.py", "class Dog(Animal):\n    pass\n")
    parsed = knowledge.parse_file(path, "python")
    assert parsed.inherits == [("Dog", "Animal", "extends")]


def test_python_multiple_bases_and_dotted_base(tmp_path):
    path = _write(tmp_path, "a.py", "class Foo(mod.Base, Mixin):\n    pass\n")
    parsed = knowledge.parse_file(path, "python")
    assert set(parsed.inherits) == {("Foo", "Base", "extends"), ("Foo", "Mixin", "extends")}


def test_python_no_bases_produces_no_inherits(tmp_path):
    path = _write(tmp_path, "a.py", "class Foo:\n    pass\n")
    parsed = knowledge.parse_file(path, "python")
    assert parsed.inherits == []


def test_javascript_class_extends(tmp_path):
    path = _write(tmp_path, "a.js", "class Dog extends Animal {}\n")
    parsed = knowledge.parse_file(path, "javascript")
    assert parsed.inherits == [("Dog", "Animal", "extends")]


def test_javascript_class_without_heritage_produces_no_inherits(tmp_path):
    path = _write(tmp_path, "a.js", "class Dog {}\n")
    parsed = knowledge.parse_file(path, "javascript")
    assert parsed.inherits == []


def test_typescript_class_extends_and_implements(tmp_path):
    path = _write(tmp_path, "a.ts", "class Foo extends Bar implements Baz, Qux {}\n")
    parsed = knowledge.parse_file(path, "typescript")
    assert set(parsed.inherits) == {
        ("Foo", "Bar", "extends"), ("Foo", "Baz", "implements"), ("Foo", "Qux", "implements"),
    }


def test_typescript_interface_extends_multiple(tmp_path):
    path = _write(tmp_path, "a.ts", "interface Foo extends Bar, Baz {}\n")
    parsed = knowledge.parse_file(path, "typescript")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "Baz", "extends")}


def test_tsx_class_extends_and_implements(tmp_path):
    path = _write(tmp_path, "a.tsx", "class Foo extends Bar implements Baz {}\n")
    parsed = knowledge.parse_file(path, "tsx")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "Baz", "implements")}


def test_java_class_extends_and_implements(tmp_path):
    path = _write(tmp_path, "Foo.java", "class Foo extends Bar implements Baz, Qux {}\n")
    parsed = knowledge.parse_file(path, "java")
    assert set(parsed.inherits) == {
        ("Foo", "Bar", "extends"), ("Foo", "Baz", "implements"), ("Foo", "Qux", "implements"),
    }


def test_java_interface_extends_multiple(tmp_path):
    path = _write(tmp_path, "Foo.java", "interface Foo extends Bar, Baz {}\n")
    parsed = knowledge.parse_file(path, "java")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "Baz", "extends")}


def test_csharp_class_base_and_interface(tmp_path):
    path = _write(tmp_path, "Foo.cs", "class Foo : Bar, IBaz { }\n")
    parsed = knowledge.parse_file(path, "csharp")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "IBaz", "implements")}


def test_csharp_class_implementing_only_interfaces(tmp_path):
    # No base class at all -- first (and only) entry looks like an interface
    # by the I<Upper> convention, so it's correctly "implements", not "extends".
    path = _write(tmp_path, "Foo.cs", "class Foo : IBar { }\n")
    parsed = knowledge.parse_file(path, "csharp")
    assert parsed.inherits == [("Foo", "IBar", "implements")]


def test_csharp_interface_extends_is_always_extends(tmp_path):
    path = _write(tmp_path, "IFoo.cs", "interface IFoo : IBar { }\n")
    parsed = knowledge.parse_file(path, "csharp")
    assert parsed.inherits == [("IFoo", "IBar", "extends")]


def test_php_class_extends_and_implements(tmp_path):
    path = _write(tmp_path, "Foo.php", "<?php\nclass Foo extends Bar implements Baz, Qux {}\n")
    parsed = knowledge.parse_file(path, "php")
    assert set(parsed.inherits) == {
        ("Foo", "Bar", "extends"), ("Foo", "Baz", "implements"), ("Foo", "Qux", "implements"),
    }


def test_php_interface_extends_multiple(tmp_path):
    path = _write(tmp_path, "Foo.php", "<?php\ninterface Foo extends Bar, Baz {}\n")
    parsed = knowledge.parse_file(path, "php")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "Baz", "extends")}


def test_cpp_class_multiple_bases_all_extends(tmp_path):
    path = _write(tmp_path, "foo.cpp", "class Foo : public Bar, private Baz {};\n")
    parsed = knowledge.parse_file(path, "cpp")
    assert set(parsed.inherits) == {("Foo", "Bar", "extends"), ("Foo", "Baz", "extends")}


def test_cpp_struct_inherits(tmp_path):
    path = _write(tmp_path, "foo.cpp", "struct Foo : Bar {};\n")
    parsed = knowledge.parse_file(path, "cpp")
    assert parsed.inherits == [("Foo", "Bar", "extends")]


def test_c_has_no_inherit_extractor(tmp_path):
    path = _write(tmp_path, "foo.c", "struct Foo { int x; };\n")
    parsed = knowledge.parse_file(path, "c")
    assert parsed.inherits == []


# --- is_test_file heuristic ------------------------------------------------

def test_is_test_file_snake_case_prefix_and_suffix():
    assert knowledge.is_test_file("test_foo.py") is True
    assert knowledge.is_test_file("foo_test.py") is True
    assert knowledge.is_test_file("foo.py") is False


def test_is_test_file_directory_marker():
    assert knowledge.is_test_file("tests/foo.py") is True
    assert knowledge.is_test_file("src/__tests__/foo.js") is True
    assert knowledge.is_test_file("src/foo.py") is False


def test_is_test_file_dotted_spec():
    assert knowledge.is_test_file("foo.spec.ts") is True
    assert knowledge.is_test_file("foo.test.js") is True


def test_is_test_file_pascal_case_suffix():
    assert knowledge.is_test_file("FooServiceTest.java") is True
    assert knowledge.is_test_file("FooServiceTests.cs") is True
    assert knowledge.is_test_file("FooService.java") is False


def test_is_test_file_does_not_false_positive_on_substring():
    assert knowledge.is_test_file("latest.py") is False


# --- scan() integration: edges actually get created and surfaced ---------

def test_scan_creates_extends_edge_and_render_mermaid_shows_it(git_repo):
    _write(git_repo, "a.py", "class Animal:\n    pass\n\nclass Dog(Animal):\n    pass\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add classes"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    stats = knowledge.stats(git_repo)
    assert stats["edges"].get("extends") == 1

    diagram = knowledge.render_mermaid(git_repo, detail="symbols", file="a.py")
    assert "--|>" in diagram


def test_scan_double_tags_test_file_calls_as_tests(git_repo):
    _write(
        git_repo, "test_foo.py",
        "def helper():\n    return 1\n\ndef test_helper():\n    return helper()\n",
    )
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add test file"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    stats = knowledge.stats(git_repo)
    assert stats["edges"].get("tests") == 1
    assert stats["edges"].get("calls") == 1  # the original calls edge is kept, not replaced


def test_scan_does_not_tag_non_test_file_calls_as_tests(git_repo):
    _write(git_repo, "a.py", "def helper():\n    return 1\n\ndef run():\n    return helper()\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add file"], cwd=git_repo, check=True)

    knowledge.scan(git_repo)
    stats = knowledge.stats(git_repo)
    assert stats["edges"].get("tests") is None
