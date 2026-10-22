"""Optional per-project exclusions for the knowledge-graph file walk, beyond
the hardcoded `VENDOR_DIR_NAMES` set in knowledge.py. Lives at
`.devmemory/ignore.json`:

    {"exclude_dirs": ["generated", "vendor"], "exclude_globs": ["**/*.min.js"]}

Read fresh on each scan -- it's a small file, and caching it would risk
acting on a stale copy across a long-lived process for no real benefit.
Missing or malformed config is treated as "no extra exclusions", not an
error: this file is strictly additive/optional.
"""

import json
from fnmatch import fnmatch
from pathlib import Path

IGNORE_CONFIG_RELATIVE_PATH = Path(".devmemory") / "ignore.json"

_EMPTY = {"exclude_dirs": set(), "exclude_globs": []}


def load_ignore_config(root) -> dict:
    path = Path(root) / IGNORE_CONFIG_RELATIVE_PATH
    if not path.exists():
        return dict(_EMPTY)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY)
    if not isinstance(data, dict):
        return dict(_EMPTY)
    return {
        "exclude_dirs": set(data.get("exclude_dirs") or []),
        "exclude_globs": list(data.get("exclude_globs") or []),
    }


def is_excluded(relative_path: Path, config: dict) -> bool:
    """`relative_path` is relative to the repo root, e.g. Path('services/backend/secrets.py')."""
    if any(part in config["exclude_dirs"] for part in relative_path.parts):
        return True
    posix = relative_path.as_posix()
    return any(fnmatch(posix, pattern) for pattern in config["exclude_globs"])
