import stat
import subprocess

import pytest

from devmemory.adapters import githook


def test_install_creates_executable_post_commit_hook(git_repo):
    hook_path = githook.install(git_repo)

    assert hook_path == git_repo / ".git" / "hooks" / "post-commit"
    content = hook_path.read_text()
    assert content.startswith("#!/bin/sh")
    assert githook.MARKER_START in content
    assert "devmemory extract" in content
    assert hook_path.stat().st_mode & stat.S_IXUSR


def test_install_preserves_existing_hook_content(git_repo):
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"
    hook_path.write_text("#!/bin/sh\necho custom\n")

    githook.install(git_repo)

    content = hook_path.read_text()
    assert "echo custom" in content
    assert "devmemory extract" in content


def test_install_is_idempotent_no_duplicate_markers(git_repo):
    githook.install(git_repo)
    githook.install(git_repo)

    content = (git_repo / ".git" / "hooks" / "post-commit").read_text()
    assert content.count(githook.MARKER_START) == 1
    assert content.count(githook.MARKER_END) == 1


def test_install_adds_shebang_when_file_preexists_without_one(git_repo):
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "post-commit"
    hook_path.write_text("echo no shebang here\n")

    githook.install(git_repo)

    content = hook_path.read_text()
    assert content.startswith("#!/bin/sh")
    assert "echo no shebang here" in content


def test_install_raises_when_not_a_git_repo(tmp_path):
    with pytest.raises(RuntimeError):
        githook.install(tmp_path)


def test_install_resolves_shared_hooks_dir_for_worktrees(git_repo):
    (git_repo / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "a.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add a"], cwd=git_repo, check=True)

    worktree_dir = git_repo.parent / "worktree"
    subprocess.run(["git", "worktree", "add", str(worktree_dir)], cwd=git_repo, check=True)

    hook_path = githook.install(worktree_dir)

    assert hook_path == git_repo / ".git" / "hooks" / "post-commit"
