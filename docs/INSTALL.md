# Installing devmemory

## Supported environments

- **Linux** and **macOS**, x86_64 or arm64, via `install.sh`.
- **Windows**: not supported by `install.sh` yet. Use WSL (follow the Linux
  instructions inside it), or install manually with `pip` (below) — devmemory
  is pure Python and has no OS-specific code.
- **Python**: 3.10, 3.11, or 3.12. `install.sh` doesn't require a
  pre-existing Python on your `PATH` for the install step itself (it uses
  `uv`, which manages its own Python), but `curl` must be available.

## One-command install

```bash
curl -fsSL https://raw.githubusercontent.com/hiKritiAnand/devmemory-project/main/install.sh \
  -o /tmp/devmemory-install.sh
bash /tmp/devmemory-install.sh
```

Read `/tmp/devmemory-install.sh` before running it — that's good practice
for any script you pipe into a shell, and the script is short (under 150
lines) specifically so this is easy. It will not:

- ask for or require `sudo`,
- disable TLS certificate verification anywhere,
- `eval` anything it downloads,
- touch any project's `.devmemory/` directory (installing the global CLI
  and indexing a repository with it are two separate actions).

## What the installer actually does

1. Detects your OS/architecture; exits with a clear error on anything
   other than Linux/macOS on x86_64/arm64.
2. Checks whether `devmemory` is already installed (and tells you so).
3. Checks for `uv` on `PATH`; if missing, installs it via its own official
   installer (`https://astral.sh/uv/install.sh`) — never assumes a system
   package manager.
4. Runs `uv tool install --from git+https://github.com/hiKritiAnand/devmemory-project.git devmemory`
   (or the same with `--force` if a previous install was detected).
5. Verifies the `devmemory` binary is on `PATH` and runs `devmemory --version`.
6. Prints next steps.

`uv tool install` installs devmemory and its dependencies into an isolated
environment it manages — nothing is installed into your system/global
Python.

## Upgrading

Re-running the installer is always safe — it detects an existing install
and reinstalls (`uv tool install --force --from git+...`). This only
replaces the global CLI; it never deletes or modifies any project's
`.devmemory/` directory. You can also upgrade directly:

```bash
uv tool install --force --from git+https://github.com/hiKritiAnand/devmemory-project.git devmemory
```

## Uninstalling

```bash
uv tool uninstall devmemory
```

This only removes the global CLI. Any project's `.devmemory/` directory is
untouched — delete it by hand (`rm -rf .devmemory`) in a specific project if
you want to remove that project's memory too.

## Installing a specific version

```bash
# Latest development revision (what `install.sh` does)
uv tool install --from git+https://github.com/hiKritiAnand/devmemory-project.git devmemory

# A specific tagged release
uv tool install --from "git+https://github.com/hiKritiAnand/devmemory-project.git@v0.2.0" devmemory

# A specific commit, for reproducible debugging
uv tool install --from "git+https://github.com/hiKritiAnand/devmemory-project.git@<commit-sha>" devmemory
```

## PATH troubleshooting

If `devmemory --version` isn't found right after installing:

```bash
uv tool update-shell   # adds uv's tool-install bin dir to your shell profile
```

then open a new terminal (or `source` your shell's profile file) and try
again. `uv tool dir --bin` prints the exact directory devmemory's binary
lives in if you want to check or add it to `PATH` manually.

## Manual installation (no `uv`, any OS Python 3.10+ supports)

```bash
pip install "git+https://github.com/hiKritiAnand/devmemory-project.git"
# with the optional MCP server:
pip install "devmemory[mcp] @ git+https://github.com/hiKritiAnand/devmemory-project.git"
```

This is the right path on Windows today, or anywhere `install.sh` doesn't
support, or inside a project's own virtual environment instead of a global
tool install.

## Offline / restricted-network limitations

`install.sh` needs network access twice: to fetch `uv` itself (if not
already present) and to fetch devmemory's source from GitHub. There is no
offline installation path today beyond manually copying the repository and
running `pip install .` against a local checkout with its dependencies
(`tree-sitter`, `tree-sitter-language-pack`, and optionally `mcp`) already
available in your local package index/cache.
