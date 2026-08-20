---
description: Search your Claude Code session history (full-text, always fresh)
argument-hint: search "query" [--current|--project P|--role R|--since D|--until D] | sessions | stats | index --full
allowed-tools: Bash(python3:*)
---

Search the user's local Claude Code session history using the claude-scope tool.

## How to run it

Use the Bash tool to run:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/scope.py" $ARGUMENTS
```

If `${CLAUDE_PLUGIN_ROOT}` is not set or did not expand in this context, locate `scope.py` by trying, in order, and use the first absolute path found:

1. The `CLAUDE_PLUGIN_ROOT` environment variable at runtime (e.g. `"$CLAUDE_PLUGIN_ROOT/scripts/scope.py"`).
2. A glob like `~/.claude/plugins/*/claude-scope/scripts/scope.py`.
3. `find ~/.claude -name scope.py -path '*claude-scope*'`.

## Interpreting the arguments

The user's input is: `$ARGUMENTS`

- Valid subcommands are `search`, `sessions`, `stats`, and `index`.
- If the input does not start with one of those subcommands (e.g. the user typed bare words like `/claude-scope:scope database migration`), treat the entire input as a search query: prepend `search` and pass the rest through unchanged.
- If the input is empty, run `sessions` to show recent sessions.

## Presenting results

- Show the script's stdout to the user essentially verbatim, inside a code block — it is pre-formatted plain text (timestamps, project, branch, role, highlighted snippets, session ids).
- After the results, add one short line: to open a found session, run `claude --resume <session-id>`.
- If the script prints a staleness WARNING, surface it prominently at the top of your response — never hide or paraphrase it away.

## Rules

- Never modify anything under `~/.claude/projects`; the script is strictly read-only there, and you must be too.
- Do not retry with destructive workarounds if the script fails; report the error output instead.
