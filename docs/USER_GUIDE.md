# User guide

This walks through using devmemory in an existing project, start to finish.
For install instructions see [INSTALL.md](INSTALL.md). For the full command
reference see the main [README.md](../README.md).

## 1. Initialize

```bash
cd /path/to/your/project
devmemory init
```

Creates `.devmemory/memory.db` (a SQLite index) and `.devmemory/records/`
(a human-readable Markdown mirror of every decision) in the current
project. Safe to run again later — it never wipes existing data, and
every schema change self-heals automatically on the next connection
(or explicitly via `devmemory migrate`).

If you're introducing devmemory into a project that already has history —
a README, a `CLAUDE.md`, ADRs, existing tests — run the retrospective
onboarding step too:

```bash
devmemory onboard --dry-run   # see what would be indexed first
devmemory onboard
```

This discovers and links existing context (README/AGENTS.md/CLAUDE.md/
cursor rules/copilot instructions/ADRs/OpenAPI specs/migration directories)
to the code graph, wherever an exact, unambiguous link exists. Running it
again on an unchanged repo indexes nothing new — safe to re-run.

## 2. First scan

```bash
devmemory scan        # same as `devmemory knowledge scan`
```

Parses the repository (12 languages: Python, JS, TS/TSX, Java, C#, Go,
PHP, C, C++, HTML, CSS, SQL) and builds the code-structure graph —
functions, classes, imports, calls, inheritance. Incremental: re-running
it only reparses files whose content actually changed.

## 3. Check what's there

```bash
devmemory status
```

Shows the project root, current git branch, schema version, how many
decisions are recorded, and the graph's node/edge counts — a quick "what's
the state of this project's memory" snapshot.

```bash
devmemory doctor
```

A health check: Python version, whether the store is initialized, whether
SQLite has FTS5 (full-text search) available, whether this is a git repo,
whether the optional `mcp` extra is installed, whether the graph has ever
been scanned. Exits non-zero if anything needs attention, so it's safe to
use in a script.

## 4. Record decisions as you work

```bash
devmemory record \
  --file src/billing/invoice.py --symbol calculate_total \
  --summary "Switched total calc to decimal.Decimal" \
  --why "float rounding caused off-by-a-cent invoices" \
  --rejected "Tried rounding at display time only; didn't fix stored totals"
```

If you also use Claude Code or Cursor, `devmemory install claude` /
`devmemory install cursor` wires up automatic recording (a git post-commit
hook plus, for Claude Code, a Stop-hook nudge) so this mostly happens for
you — see the README's "Automatic recording" section.

## 5. Query before editing

```bash
# Scoped to exactly the function you're about to touch
devmemory query --file src/billing/invoice.py --symbol calculate_total

# Free text, when you don't know (or don't remember) where the logic lives
devmemory query --text "payment retries"
devmemory explain "why was decimal used for invoice totals"
devmemory memory query "payment retries"
```

`explain`/`memory query` are keyword search over recorded decisions (and,
for `explain`, onboarded project context too) — not natural-language
understanding. They're meant to be a quick first pass; `query --file
--symbol` is the precise, scoped lookup.

```bash
# What would be affected by changing this symbol
devmemory knowledge impact --file src/billing/invoice.py --symbol calculate_total

# Task-specific traversal with more control
devmemory context --file src/billing/invoice.py --symbol calculate_total --profile bug_fix
devmemory profiles   # lists all 8 available profiles
```

## 6. Keep the graph current

```bash
devmemory scan
```

Run this after pulling changes or at the start of a session — it's cheap
(incremental, skips unchanged files) and keeps `impact`/`context`/`inspect`
accurate. `devmemory install git`'s post-commit hook does this
automatically after every commit.

## 7. Data storage, cleanup, and privacy

Everything lives under `.devmemory/` in the project root: `memory.db`
(SQLite — gitignored by default, see the README's security section), and
`records/*.md` (the Markdown decision mirror — meant to be git-tracked,
human-readable, grep-able history). To remove a project's devmemory data
entirely: `rm -rf .devmemory`. There's no separate "clean" command —
deleting the directory is the complete, safe operation (nothing devmemory
creates lives outside it).

## 8. Common troubleshooting

- **"devmemory store not initialized"** — run `devmemory init` first.
- **A schema-related error after upgrading devmemory** — run `devmemory
  migrate` explicitly (though every command already self-heals the schema
  automatically on connect).
- **`query --text` seems less precise than expected** — check `devmemory
  doctor`'s FTS5 line; some SQLite builds lack FTS5, in which case search
  falls back to a plain `LIKE` match automatically (same command, slightly
  less precise ranking).
- **A decision you recorded isn't showing up in `query`** — decisions are
  scoped to the current git branch by default; add `--all-branches`, or
  check `devmemory status`'s branch line.
