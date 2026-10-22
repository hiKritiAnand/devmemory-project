<!-- devmemory:start -->
## devmemory

This project uses `devmemory` to persist decision records that capture
*why* code changed, not just what changed, so context survives across
sessions and across tools (Claude Code, Cursor, etc).

Before editing an existing function, check for relevant prior history:

    devmemory query --file <path> --symbol <name>

After making a change that involved a non-obvious tradeoff, a rejected
alternative, or a decision future-you will want to remember, record it:

    devmemory record --file <path> --symbol <name> \
        --summary "<what changed>" --why "<why>" --rejected "<what was rejected>"

To see what changed recently, with no LLM call required:

    devmemory log --since <date>
<!-- devmemory:end --