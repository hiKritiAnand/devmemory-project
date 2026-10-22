#!/usr/bin/env bash
#
# devmemory installer.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/hiKritiAnand/devmemory-project/main/install.sh \
#     -o /tmp/devmemory-install.sh
#   bash /tmp/devmemory-install.sh
#
# This script is short and meant to be read before you run it -- the same
# way you'd inspect any script you pipe into a shell. It:
#   - does NOT require root/sudo (uv installs tools into your user's own
#     directories, never system-wide),
#   - does NOT disable TLS verification anywhere,
#   - does NOT eval() any external input,
#   - does NOT execute anything from inside a repository it indexes --
#     installing the global `devmemory` CLI and indexing a project with it
#     are two entirely separate actions; this script only does the former,
#   - never touches any project's `.devmemory/` directory (that's created
#     per-repository by `devmemory init`, independent of where the CLI
#     itself is installed).
#
# Supported: Linux and macOS, x86_64/arm64. Not supported here: Windows --
# use WSL, or install manually (`pip install` works anywhere Python 3.10+
# does; see docs/INSTALL.md).

set -euo pipefail

REPO_URL="https://github.com/hiKritiAnand/devmemory-project.git"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

log()  { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

log "devmemory installer"
log "===================="

# --- 1. OS / architecture detection ---------------------------------------

os_name="$(uname -s 2>/dev/null || echo unknown)"
arch_name="$(uname -m 2>/dev/null || echo unknown)"

case "$os_name" in
    Linux|Darwin) ;;
    *)
        die "Unsupported operating system: '$os_name'. This installer currently supports Linux and macOS only. A plain 'pip install git+${REPO_URL}' works anywhere Python 3.10+ does -- see docs/INSTALL.md."
        ;;
esac

case "$arch_name" in
    x86_64|amd64|arm64|aarch64) ;;
    *)
        die "Unsupported architecture: '$arch_name'. devmemory is pure Python; a manual 'pip install git+${REPO_URL}' should still work -- see docs/INSTALL.md."
        ;;
esac

log "Detected platform: $os_name ($arch_name)"

# --- 2. Already installed? --------------------------------------------------

already_installed=0
if command -v devmemory >/dev/null 2>&1; then
    already_installed=1
    existing_version="$(devmemory --version 2>/dev/null || echo 'an unknown version')"
    log "devmemory is already installed ($existing_version) -- this run will reinstall/upgrade it."
fi

# --- 3. Locate or bootstrap uv ----------------------------------------------
#
# devmemory's dependencies (tree-sitter, etc.) are installed into an
# isolated environment uv manages for this one tool -- never into your
# system/global Python.

if command -v uv >/dev/null 2>&1; then
    log "Found uv: $(uv --version)"
else
    log "uv not found on PATH -- installing it via its official installer:"
    log "  $UV_INSTALL_URL"
    if ! command -v curl >/dev/null 2>&1; then
        die "curl is required to bootstrap uv but was not found. Install curl, or install uv yourself (see https://docs.astral.sh/uv/getting-started/installation/) and re-run this script."
    fi
    curl -LsSf "$UV_INSTALL_URL" | sh

    # uv's own installer places the binary under ~/.local/bin (or
    # ~/.cargo/bin on some releases) and does not necessarily update the
    # current shell session's PATH -- only future ones, via a profile edit
    # it makes itself. Extend PATH for the rest of *this* script only.
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

    if ! command -v uv >/dev/null 2>&1; then
        die "uv finished installing but is still not on PATH in this shell. Open a new terminal (or re-source your shell profile) and re-run this installer."
    fi
    log "Installed uv: $(uv --version)"
fi

# --- 4. Install (or reinstall/upgrade) devmemory ----------------------------

if [ "$already_installed" -eq 1 ]; then
    log "Reinstalling devmemory from $REPO_URL ..."
    log "(this only replaces the global CLI -- no project's .devmemory/ data is touched)"
    uv tool install --force --from "git+${REPO_URL}" devmemory
else
    log "Installing devmemory from $REPO_URL ..."
    uv tool install --from "git+${REPO_URL}" devmemory
fi

# --- 5. Verify ---------------------------------------------------------

if ! command -v devmemory >/dev/null 2>&1; then
    tool_bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
    die "devmemory installed, but '$tool_bin_dir' isn't on your PATH yet. Run 'uv tool update-shell', open a new shell, then run 'devmemory --version' to confirm."
fi

installed_version="$(devmemory --version)"
log ""
log "devmemory installed: $installed_version"
log ""
log "Next steps, in any existing project:"
log "  cd /path/to/your/project"
log "  devmemory init"
log "  devmemory scan"
log "  devmemory status"
log "  devmemory explain \"<a question about this codebase>\""
log ""
log "Full docs: https://github.com/hiKritiAnand/devmemory-project#readme"
