import json

from devmemory import ignore_config


def test_missing_config_returns_empty(tmp_path):
    cfg = ignore_config.load_ignore_config(tmp_path)
    assert cfg == {"exclude_dirs": set(), "exclude_globs": []}


def test_malformed_config_returns_empty(tmp_path):
    (tmp_path / ".devmemory").mkdir()
    (tmp_path / ".devmemory" / "ignore.json").write_text("not json{{{")

    cfg = ignore_config.load_ignore_config(tmp_path)
    assert cfg == {"exclude_dirs": set(), "exclude_globs": []}


def test_loads_exclude_dirs_and_globs(tmp_path):
    (tmp_path / ".devmemory").mkdir()
    (tmp_path / ".devmemory" / "ignore.json").write_text(
        json.dumps({"exclude_dirs": ["generated"], "exclude_globs": ["**/*.min.js"]})
    )

    cfg = ignore_config.load_ignore_config(tmp_path)
    assert cfg["exclude_dirs"] == {"generated"}
    assert cfg["exclude_globs"] == ["**/*.min.js"]


def test_is_excluded_by_dir():
    from pathlib import Path

    cfg = {"exclude_dirs": {"generated"}, "exclude_globs": []}
    assert ignore_config.is_excluded(Path("src/generated/foo.py"), cfg) is True
    assert ignore_config.is_excluded(Path("src/real/foo.py"), cfg) is False


def test_is_excluded_by_glob():
    from pathlib import Path

    cfg = {"exclude_dirs": set(), "exclude_globs": ["*.min.js"]}
    assert ignore_config.is_excluded(Path("vendor/lib.min.js"), cfg) is True
    assert ignore_config.is_excluded(Path("vendor/lib.js"), cfg) is False
