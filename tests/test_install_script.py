"""Targeted tests for install.sh's OS/arch detection gate. Deliberately NOT
a full installer integration test (fresh install / upgrade / network
failure): faking `uv`/network realistically in CI is its own project, and
would mean either mocking a real package manager or actually hitting the
network in a test run. Instead: a fake `uname` on a restricted PATH (no
`curl`, no `uv`) drives the script up to its first external-tool check and
no further, so these tests run instantly, offline, and never install
anything on the test machine.
"""

import shutil
import subprocess
from pathlib import Path

INSTALL_SH = Path(__file__).parent.parent / "install.sh"
_BASH = shutil.which("bash")  # resolved once via the test process's own PATH,
                              # since the script itself will run with a
                              # deliberately restricted one


def _run_with_fake_uname(tmp_path, os_name: str, arch_name: str):
    fake_uname = tmp_path / "uname"
    fake_uname.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        f'    -s) echo "{os_name}" ;;\n'
        f'    -m) echo "{arch_name}" ;;\n'
        '    *) echo "" ;;\n'
        "esac\n"
    )
    fake_uname.chmod(0o755)
    # PATH has only the fake `uname` -- no real `uv`/`curl`/`devmemory` are
    # reachable, so the script can never reach an actual network call here.
    return subprocess.run(
        [_BASH, str(INSTALL_SH)],
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=10,
    )


def _code_lines() -> str:
    """install.sh's content with comment-only lines stripped, so a search
    for a dangerous command isn't fooled by a comment that merely *mentions*
    it (e.g. "does NOT eval() external input")."""
    return "\n".join(
        line for line in INSTALL_SH.read_text().splitlines()
        if not line.strip().startswith("#")
    )


def test_install_script_syntax_is_valid():
    result = subprocess.run([_BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_install_script_rejects_unsupported_os(tmp_path):
    result = _run_with_fake_uname(tmp_path, "SunOS", "x86_64")
    assert result.returncode == 1
    assert "Unsupported operating system" in result.stderr


def test_install_script_rejects_unsupported_arch(tmp_path):
    result = _run_with_fake_uname(tmp_path, "Linux", "ppc64")
    assert result.returncode == 1
    assert "Unsupported architecture" in result.stderr


def test_install_script_accepts_linux_x86_64(tmp_path):
    result = _run_with_fake_uname(tmp_path, "Linux", "x86_64")
    assert "Detected platform: Linux (x86_64)" in result.stdout
    # No curl/uv reachable on the restricted PATH -- it must fail cleanly
    # at that explicit, documented check, not crash or hang on a network call.
    assert result.returncode == 1
    assert "curl is required" in result.stderr


def test_install_script_accepts_macos_arm64(tmp_path):
    result = _run_with_fake_uname(tmp_path, "Darwin", "arm64")
    assert "Detected platform: Darwin (arm64)" in result.stdout


def test_install_script_never_disables_tls_verification():
    code = _code_lines()
    assert "-k " not in code and "--insecure" not in code


def test_install_script_never_uses_eval():
    assert "eval" not in _code_lines()


def test_install_script_has_strict_error_handling():
    assert "set -euo pipefail" in INSTALL_SH.read_text()
