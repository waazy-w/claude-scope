#!/usr/bin/env python3
"""scope -- search your Claude Code session history.

CLI front-end for the claude-scope plugin. Talks to the local SQLite/FTS5
index maintained by scope_indexer.py. Read-only with respect to
~/.claude/projects; all writes go to the index database.

Subcommands:
  search    full-text search across all indexed sessions
  sessions  list recent sessions
  index     refresh (or rebuild) the index
  stats     show index statistics

Plain-text output only: no colors, no cursor tricks, renders identically
in a terminal and inside IDE extensions.
"""

import argparse
import datetime
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scope_indexer  # noqa: E402


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def parse_iso_utc(ts):
    """Parse an ISO8601 UTC string like 2026-07-19T19:32:39.444Z.

    Returns an aware datetime, or None if it cannot be parsed.
    """
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        # last resort: strip fractional seconds of unusual width
        m = re.match(r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})", s)
        if not m:
            return None
        try:
            dt = datetime.datetime.fromisoformat(m.group(1))
        except ValueError:
            return None
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def fmt_local(ts, with_seconds=False):
    """Render an ISO UTC timestamp as local time 'YYYY-MM-DD HH:MM'."""
    dt = parse_iso_utc(ts)
    if dt is None:
        return str(ts) if ts else "(no timestamp)"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return dt.astimezone().strftime(fmt)


def human_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            if unit == "B":
                return "%d %s" % (int(n), unit)
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%d B" % n


def collapse_ws(text):
    return re.sub(r"\s+", " ", text or "").strip()


def truncate(text, width=80):
    text = collapse_ws(text)
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


def display_project(cwd, project_dir):
    """Human-readable project name: basename of cwd, else raw project_dir."""
    if cwd:
        base = os.path.basename(cwd.rstrip("/"))
        if base:
            return base
    return project_dir or "(unknown project)"


DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


def _to_utc_bound(dt):
    """Format an aware datetime as an ISO UTC string comparable
    (lexicographically) to the stored timestamps."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def normalize_date_arg(value, is_until):
    """Validate --since/--until and return a string comparable to the
    stored ISO8601 UTC timestamps (lexicographic comparison).

    Bare dates and datetimes without an explicit offset are interpreted in
    LOCAL time (matching the local timestamps this tool displays) and
    converted to UTC bounds.
    """
    v = value.strip()
    if DATE_ONLY_RE.match(v):
        try:
            day = datetime.date.fromisoformat(v)
        except ValueError:
            raise UsageError("invalid date %r for %s"
                             % (value, "--until" if is_until else "--since"))
        t = datetime.time(23, 59, 59, 999999) if is_until else datetime.time(0, 0)
        local = datetime.datetime.combine(day, t).astimezone()  # naive -> local tz
        return _to_utc_bound(local)
    if ISO_RE.match(v):
        s = v.replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # fromisoformat (3.9) needs a colon in the offset
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            raise UsageError("invalid date %r for %s"
                             % (value, "--until" if is_until else "--since"))
        if dt.tzinfo is None:
            dt = dt.astimezone()  # interpret as local time
        return _to_utc_bound(dt)
    raise UsageError(
        "invalid date %r for %s (expected YYYY-MM-DD or full ISO8601)"
        % (value, "--until" if is_until else "--since")
    )


class UsageError(Exception):
    """User/usage error -> exit code 1."""


class Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad usage by default; we want exit code 1."""

    def error(self, message):
        self.print_usage(sys.stderr)
        sys.stderr.write("%s: error: %s\n" % (self.prog, message))
        sys.exit(1)


# ---------------------------------------------------------------------------
# freshness / sync
# ---------------------------------------------------------------------------

def refresh_index(conn, skip=False):
    """Incrementally sync the index before serving a query.

    Prints a one-line freshness note. Never raises: on failure prints a
    prominent warning so we never silently serve stale results.
    """
    if skip:
        print("[index sync skipped (--no-sync): results may be stale]")
        return
    try:
        result = scope_indexer.sync(conn)
    except Exception as exc:  # noqa: BLE001 - freshness must never crash search
        print("WARNING: index may be stale: sync failed: %s" % exc)
        return
    errors = result.get("errors") or []
    added = result.get("messages_added", 0) or 0
    updated = result.get("files_updated", 0) or 0
    removed = result.get("files_removed", 0) or 0
    if added or updated or removed:
        parts = ["+%d messages from %d files" % (added, updated)]
        if removed:
            parts.append("%d files removed" % removed)
        print("[index refreshed: %s]" % ", ".join(parts))
    else:
        print("[index fresh]")
    if errors:
        shown = "; ".join(str(e) for e in errors[:3])
        if len(errors) > 3:
            shown += " (and %d more)" % (len(errors) - 3)
        print("WARNING: index may be stale: %s" % shown)


# ---------------------------------------------------------------------------
# filters shared by search / sessions
# ---------------------------------------------------------------------------

def build_filters(args, prefix="messages."):
    """Return (sql_clauses, params, human_descriptions)."""
    clauses, params, desc = [], [], []
    if getattr(args, "current", False):
        enc = scope_indexer.encode_project_dir(os.getcwd())
        clauses.append("%sproject_dir = ?" % prefix)
        params.append(enc)
        desc.append("project=current (%s)" % os.getcwd())
    if getattr(args, "project", None):
        p = args.project
        if "/" in p:
            enc = scope_indexer.encode_project_dir(
                os.path.abspath(os.path.expanduser(p))
            )
            clauses.append("%sproject_dir = ?" % prefix)
            params.append(enc)
            desc.append("project=%s" % p)
        else:
            like = "%" + p + "%"
            clauses.append(
                "(%sproject_dir LIKE ? OR %scwd LIKE ?)" % (prefix, prefix)
            )
            params.extend([like, like])
            desc.append("project~%s" % p)
    if getattr(args, "role", None):
        clauses.append("%srole = ?" % prefix)
        params.append(args.role)
        desc.append("role=%s" % args.role)
    if getattr(args, "since", None):
        clauses.append("%stimestamp >= ?" % prefix)
        params.append(normalize_date_arg(args.since, is_until=False))
        desc.append("since=%s" % args.since)
    if getattr(args, "until", None):
        clauses.append("%stimestamp <= ?" % prefix)
        params.append(normalize_date_arg(args.until, is_until=True))
        desc.append("until=%s" % args.until)
    return clauses, params, desc


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def tokens_to_fts(query):
    """Safe FTS5 query: each whitespace token double-quoted (implicit AND)."""
    toks = query.split()
    return " ".join('"%s"' % t.replace('"', '""') for t in toks)


def looks_like_fts(query):
    """Heuristic: the user seems to be writing raw FTS5 syntax."""
    return bool(
        re.search(r'"', query)
        or re.search(r"\b(OR|NEAR|NOT|AND)\b", query)
        or re.search(r"[()*]", query)
        or re.search(r"\bNEAR\(", query)
    )


def cmd_search(args):
    raw_query = " ".join(args.query).strip()
    if not raw_query:
        raise UsageError("empty search query")

    clauses, params, desc = build_filters(args)  # validate filters first
    conn = scope_indexer.open_db()
    refresh_index(conn, skip=args.no_sync)
    filter_sql = ""
    if clauses:
        filter_sql = " AND " + " AND ".join(clauses)

    count_sql = (
        "SELECT COUNT(*) FROM messages_fts JOIN messages "
        "ON messages.id = messages_fts.rowid "
        "WHERE messages_fts MATCH ?" + filter_sql
    )
    select_sql = (
        "SELECT messages.session_id, messages.project_dir, messages.cwd, "
        "messages.git_branch, messages.role, messages.timestamp, "
        "snippet(messages_fts, 0, '«', '»', ' … ', 20) "
        "FROM messages_fts JOIN messages "
        "ON messages.id = messages_fts.rowid "
        "WHERE messages_fts MATCH ?" + filter_sql +
        " ORDER BY bm25(messages_fts) ASC LIMIT ?"
    )

    # Prefer the user's raw query if it looks like (and is) valid FTS5
    # syntax; otherwise use the safe token-quoted form.
    candidates = []
    if looks_like_fts(raw_query):
        candidates.append(raw_query)
    candidates.append(tokens_to_fts(raw_query))

    total = None
    rows = None
    last_err = None
    for match_expr in candidates:
        try:
            total = conn.execute(
                count_sql, [match_expr] + params
            ).fetchone()[0]
            rows = conn.execute(
                select_sql, [match_expr] + params + [args.limit]
            ).fetchall()
            break
        except sqlite3.OperationalError as exc:
            last_err = exc
            continue
    if rows is None:
        raise UsageError("could not parse search query %r: %s" % (raw_query, last_err))

    print("")
    if total == 0:
        applied = "; ".join(desc) if desc else "none"
        print("No matches for %r (filters: %s)." % (raw_query, applied))
        return 0

    shown = len(rows)
    if total > shown:
        print("showing %d of %d matches" % (shown, total))
    else:
        print("%d match%s" % (total, "" if total == 1 else "es"))
    print("")

    for i, (sid, project_dir, cwd, branch, role, ts, snip) in enumerate(rows, 1):
        header = "%d. %s · %s" % (
            i, fmt_local(ts), display_project(cwd, project_dir)
        )
        if branch:
            header += " · branch %s" % branch
        header += " · %s" % role
        print(header)
        print("   %s" % collapse_ws(snip))
        print("   session %s   (resume: claude --resume %s)" % (sid, sid))
        if i != shown:
            print("")
    return 0


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

def cmd_sessions(args):
    clauses, params, desc = build_filters(args, prefix="")
    conn = scope_indexer.open_db()
    refresh_index(conn)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT session_id, MAX(timestamp) AS last_ts, COUNT(*) AS n, "
        "MAX(cwd) AS cwd, MAX(project_dir) AS project_dir "
        "FROM messages %s GROUP BY session_id "
        "ORDER BY last_ts DESC LIMIT ?" % where
    )
    rows = conn.execute(sql, params + [args.limit]).fetchall()

    print("")
    if not rows:
        applied = "; ".join(desc) if desc else "none"
        print("No sessions found (filters: %s)." % applied)
        return 0

    for i, (sid, last_ts, n, cwd, project_dir) in enumerate(rows, 1):
        first_user = conn.execute(
            "SELECT text FROM messages WHERE session_id = ? AND role = 'user' "
            "ORDER BY timestamp ASC LIMIT 1",
            (sid,),
        ).fetchone()
        title = truncate(first_user[0], 80) if first_user and first_user[0] else "(no user message)"
        print(
            "%d. %s · %s · %d message%s"
            % (i, fmt_local(last_ts), display_project(cwd, project_dir),
               n, "" if n == 1 else "s")
        )
        print("   %s" % title)
        print("   session %s   (resume: claude --resume %s)" % (sid, sid))
        if i != len(rows):
            print("")
    return 0


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def cmd_index(args):
    conn = scope_indexer.open_db()
    mode = "full rebuild" if args.full else "incremental sync"
    print("Running %s ..." % mode)
    result = scope_indexer.sync(conn, full=args.full)
    if result.get("rebuilt_for_version") and not args.full:
        print("  (index format changed; rebuilt from scratch)")
    print("  files scanned:   %s" % result.get("files_scanned", 0))
    print("  files updated:   %s" % result.get("files_updated", 0))
    print("  files removed:   %s" % result.get("files_removed", 0))
    print("  messages added:  %s" % result.get("messages_added", 0))
    print("  malformed lines: %s" % result.get("malformed_lines", 0))
    elapsed = result.get("elapsed_s")
    if elapsed is not None:
        print("  elapsed:         %.2fs" % float(elapsed))
    errors = result.get("errors") or []
    if errors:
        print("  errors (%d):" % len(errors))
        for e in errors:
            print("    - %s" % e)
        return 1
    print("  errors:          none")
    return 0


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

def cmd_stats(args):
    conn = scope_indexer.open_db()
    refresh_index(conn)  # keep the report honest

    s = scope_indexer.stats(conn)
    by_role = s.get("by_role") or {}
    role_bits = ", ".join("%s %s" % (r, by_role[r]) for r in sorted(by_role))
    first_ts, last_ts = s.get("first_ts"), s.get("last_ts")
    if first_ts and last_ts:
        date_range = "%s → %s (local time)" % (
            fmt_local(first_ts), fmt_local(last_ts)
        )
    else:
        date_range = "(empty index)"

    print("")
    print("Index statistics")
    print("  db:        %s (%s)" % (s.get("db_path"), human_bytes(s.get("db_bytes", 0))))
    print("  files:     %s" % s.get("files", 0))
    msg_line = "  messages:  %s" % s.get("messages", 0)
    if role_bits:
        msg_line += "  (%s)" % role_bits
    print(msg_line)
    print("  projects:  %s" % s.get("projects", 0))
    print("  range:     %s" % date_range)

    rows = conn.execute(
        "SELECT project_dir, COUNT(*) AS n, MAX(cwd) AS cwd "
        "FROM messages GROUP BY project_dir ORDER BY n DESC LIMIT 15"
    ).fetchall()
    if rows:
        print("")
        print("Messages by project (top %d):" % len(rows))
        width = max(len(str(r[1])) for r in rows)
        for project_dir, n, cwd in rows:
            print("  %*d  %s" % (width, n, display_project(cwd, project_dir)))
    return 0


# ---------------------------------------------------------------------------
# argument parsing / main
# ---------------------------------------------------------------------------

def build_parser():
    parser = Parser(
        prog="scope",
        description="Search your Claude Code session history.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<subcommand>")

    def add_project_filters(p):
        p.add_argument(
            "--current", action="store_true",
            help="only the project for the current working directory",
        )
        p.add_argument(
            "--project", metavar="P",
            help="project filter: a path (contains '/') or a substring "
                 "matched against project dir and cwd",
        )

    p_search = sub.add_parser(
        "search", help="full-text search across sessions",
        description="Full-text search across all indexed session history. "
                    "The index is refreshed incrementally before every "
                    "search unless --no-sync is given.",
    )
    p_search.add_argument("query", nargs="+", help="search terms (implicit AND)")
    add_project_filters(p_search)
    p_search.add_argument("--role", choices=["user", "assistant"],
                          help="only messages with this role")
    p_search.add_argument("--since", metavar="DATE",
                          help="only messages on/after DATE (YYYY-MM-DD or ISO8601, "
                               "local time unless an offset/Z is given)")
    p_search.add_argument("--until", metavar="DATE",
                          help="only messages on/before DATE (YYYY-MM-DD or ISO8601, "
                               "day-inclusive, local time unless an offset/Z is given)")
    p_search.add_argument("--limit", type=int, default=10, metavar="N",
                          help="max results to show (default 10)")
    p_search.add_argument("--no-sync", action="store_true",
                          help="skip the pre-search index refresh "
                               "(fast, but results may be stale)")
    p_search.set_defaults(func=cmd_search)

    p_sessions = sub.add_parser(
        "sessions", help="list recent sessions",
        description="List recent sessions, newest first, with message "
                    "counts and the first user message as a title.",
    )
    add_project_filters(p_sessions)
    p_sessions.add_argument("--limit", type=int, default=15, metavar="N",
                            help="max sessions to show (default 15)")
    p_sessions.set_defaults(func=cmd_sessions)

    p_index = sub.add_parser(
        "index", help="refresh (or rebuild) the index",
        description="Incrementally sync the index with ~/.claude/projects. "
                    "With --full, rebuild it from scratch.",
    )
    p_index.add_argument("--full", action="store_true",
                         help="rebuild the index from scratch")
    p_index.set_defaults(func=cmd_index)

    p_stats = sub.add_parser(
        "stats", help="show index statistics",
        description="Show index statistics (the index is refreshed first "
                    "so the report is honest).",
    )
    p_stats.set_defaults(func=cmd_stats)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    if getattr(args, "limit", None) is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")
    try:
        return args.func(args) or 0
    except UsageError as exc:
        sys.stderr.write("scope: error: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("scope: interrupted\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - readable message, not a traceback
        sys.stderr.write(
            "scope: unexpected error (%s): %s\n" % (type(exc).__name__, exc)
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
