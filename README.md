# claude-scope

Fast, local, full-text search over your Claude Code session history. It indexes the
`.jsonl` session logs under `~/.claude/projects/` into a SQLite FTS5 database and lets
you search them from inside Claude Code (`/claude-scope:scope`) or from any shell.

## Why claude-scope

There are plenty of tools that search Claude Code history. claude-scope makes two
promises the others don't:

**1. Results are never stale — guaranteed, not hoped.** Indexed search tools serve
whatever their last indexing run saw; grep-style tools are always fresh but rescan
your entire history (easily hundreds of MB) on every query. claude-scope does
neither: it tracks a byte offset per log file, and *every search* first runs an
incremental sync that reads only the bytes appended since last time — milliseconds of
work. A message you typed one minute ago in your **current, still-running session**
is already searchable. The freshness contract is explicit and tested:

- Every result set is prefixed with `[index fresh]` or
  `[index refreshed: +N messages]` — and if the sync ever fails, a loud WARNING
  tells you exactly how stale the results are. Silent staleness is impossible.
- Half-written lines (Claude Code mid-write) are held back until complete — nothing
  missed, nothing double-counted.
- Even queued mid-turn prompts — which Claude Code logs as `attachment` lines, not
  `user` lines, and which most indexers silently drop — are indexed. Background
  task notifications, which share that same record type, are filtered out so a
  search for something *you* said doesn't surface agent chatter.

**2. It's a native plugin, not another tool to babysit.** One `claude plugin install`
and you get a slash command that works identically in the CLI and IDE extensions. No
MCP server process, no daemon, no watcher, no web UI, no Node/npm — just Python
stdlib and SQLite, both already on your machine. Zero network access, and strictly
read-only on Claude's files.

## What it looks like

```
> /claude-scope:scope search "database migration" --role user

[index refreshed: +26 messages from 1 files]

showing 2 of 2 matches

1. 2026-08-20 09:45 · myapp · branch main · user
   …how should we handle the «database» «migration» for the users table without
   downtime? I was thinking expand-contract…
   session 97894942-e7e2-4753-93fb-c4b38c5e3743   (resume: claude --resume 97894942-e7e2-4753-93fb-c4b38c5e3743)

2. 2026-08-02 14:11 · api-server · branch fix/schema · user
   …the «migration» failed halfway — can we make the «database» rollback
   script idempotent…
   session 41b2c8d0-3e5f-4a91-b7c2-8d94e1f0a623   (resume: claude --resume 41b2c8d0-3e5f-4a91-b7c2-8d94e1f0a623)
```

Note the first line: the sync ran *during this search* and picked up 26 messages
written since the last query — that's the freshness guarantee in action. Plain text
only, no TUI — identical output in the terminal and IDE extensions.

## Install

From GitHub (persistent — the repo doubles as a plugin marketplace):

```
claude plugin marketplace add waazy-w/claude-scope
claude plugin install claude-scope@claude-scope
```

From a local clone, for development / per-session use:

```
git clone https://github.com/waazy-w/claude-scope.git
claude --plugin-dir /path/to/claude-scope
```

`--plugin-dir` plugins are session-only; pass the flag each time (or keep a shell
alias).

Requirements: Python 3.9+ (macOS/Linux system `python3` is fine) with the stdlib
`sqlite3` module built with FTS5 — true almost everywhere. No other dependencies.

## Uninstall

- Marketplace installs: `claude plugin uninstall claude-scope`.
- `--plugin-dir` installs: just don't pass the flag next session — nothing is loaded.
- Optionally delete the index database (see "Where the index lives" below).

## Usage

Inside Claude Code:

```
/claude-scope:scope database migration                 # bare words = search
/claude-scope:scope search "fts5 tokenizer" --current  # only the current project (cwd)
/claude-scope:scope search "deploy" --project myapp    # filter by project
/claude-scope:scope search "error" --role user         # only your messages
/claude-scope:scope search "auth" --since 2026-07-01 --until 2026-08-01
/claude-scope:scope search "timeout" --limit 5 --no-sync
/claude-scope:scope sessions --project myapp           # recent sessions with titles
/claude-scope:scope stats                              # index stats
/claude-scope:scope index --full                       # full rebuild
```

Query syntax: plain words are ANDed together. If your query looks like FTS5 syntax
(quotes, `OR`, `NEAR`, `NOT`, parentheses, `*`), it is tried as raw FTS5 first and
falls back to literal token matching if it doesn't parse.

Directly from a shell (no Claude required):

```
python3 /path/to/claude-scope/scripts/scope.py search "query"
python3 /path/to/claude-scope/scripts/scope.py sessions --limit 20
python3 /path/to/claude-scope/scripts/scope.py index    # manual incremental index
python3 /path/to/claude-scope/scripts/scope.py stats
```

To open a session you found: `claude --resume <session-id>`.

## How freshness works

The index stores a byte offset for every log file. Before each search, a sync pass
reads only bytes appended since the last pass — usually milliseconds of work. A
half-written trailing line (Claude Code mid-write) is left alone; the offset does not
advance past it until it is complete, so nothing is missed or double-counted. Stale
results are never served silently: if the sync fails, the search prints a WARNING and
tells you how fresh the results are.

## Where the index lives

`$CLAUDE_PLUGIN_DATA/scope.db` when that variable is set, otherwise
`~/.claude/plugin-data/claude-scope/scope.db`. Override with `CLAUDE_SCOPE_DATA`.

To force a full reindex, run `index --full` — or simply delete the `.db` file; it is
rebuilt automatically on next use. When an upgrade changes the indexing rules, the
index is rebuilt once automatically on the next sync (it takes about a second per
few thousand messages). Indexing is resumable (commits per file / every ~2000
messages) and deduplicated by message uuid.

## Optional: eager indexing at session start

Plugin hooks in `hooks/hooks.json` are enabled automatically the moment a plugin is
enabled — which is why this one ships inactive, as an opt-in example. To index at
every session start (a "watcher" with no daemon):

```
mkdir -p hooks && cp hooks.examples/hooks.json hooks/hooks.json
```

To opt out again, delete `hooks/hooks.json`. Searches stay fresh either way; this
only warms the index earlier.

## Guarantees & limits

- Strictly read-only on Claude's files: logs are opened read-only, never locked,
  moved, or modified. The plugin writes only to its own database directory.
- No daemons, no network, Python 3.9+ stdlib only (SQLite with FTS5).
- Subagent sidechain logs are not indexed in v1.
- Format-stability caveat: the official docs note the session `.jsonl` format is
  internal and may change between Claude Code versions. claude-scope reads it
  defensively — unknown line types are skipped and malformed or partial lines are
  tolerated — but a future Claude Code release could still require an update.
