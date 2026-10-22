"""Small git helper shared across modules that need the current branch name
-- kept separate from extract.py (which shells out for commit/diff data, a
different concern entirely) so storage.py can use it without importing
extract.py or knowledge.py.
"""

import subprocess


def current_branch(root) -> str | None:
    """Returns the current branch name, or None if this isn't a git repo, or
    HEAD is detached (can't attribute to any specific branch) -- never
    raises, since not being in a git repo is a normal, expected case here,
    not an error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root, capture_output=True, text=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    if not branch or branch == "HEAD":  # "HEAD" here means detached HEAD, not a named branch
        return None
    return branch
