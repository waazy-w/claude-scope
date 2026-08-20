#!/usr/bin/env python3
"""claude-scope indexer: indexes Claude Code session logs into SQLite FTS5.

READ-ONLY GUARANTEE: this module never writes to, moves, locks, or otherwise
modifies anything under ~/.claude/projects/ (or any projects_dir passed in).
Session log files are opened strictly in 'rb' mode for reading only. The
index database lives in the plugin data directory (see data_dir()), never
next to the logs.

Python 3.9+, stdlib only, no network access.
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

MAX_TEXT_CHARS = 100_000
COMMIT_EVERY_MESSAGES = 2000

# Bump whenever _extract_message changes what it keeps or drops. An index
# recorded under a different version is rebuilt once on the next sync, so
# rows admitted by older rules (e.g. task notifications) do not linger.
#   1: unversioned indexes (pre-0.1.1)
#   2: queued_command filtering (skip task notifications, keep image prompts)
INDEX_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT UNIQUE NOT NULL,
  size_indexed INTEGER NOT NULL DEFAULT 0,
  mtime REAL NOT NULL DEFAULT 0,
  inode INTEGER NOT NULL DEFAULT 0,
  session_id TEXT,
  project_dir TEXT
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL,
  uuid TEXT,
  session_id TEXT NOT NULL,
  project_dir TEXT NOT NULL,
  cwd TEXT,
  git_branch TEXT,
  role TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  text TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_uuid ON messages(uuid);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(text, content='messages', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def data_dir():
    """Plugin data directory (created if missing). Never inside the logs tree."""
    p = os.environ.get("CLAUDE_SCOPE_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if p:
        d = Path(p)
    else:
        d = Path.home() / ".claude" / "plugin-data" / "claude-scope"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path():
    return data_dir() / "scope.db"


def open_db():
    conn = sqlite3.connect(str(db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def encode_project_dir(path_str):
    """Encode an absolute path the way Claude Code names project dirs."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path_str))


def _extract_message(obj):
    """Return (role, text) for an indexable user/assistant line, else None."""
    mtype = obj.get("type")
    if mtype == "user":
        if obj.get("isMeta"):
            return None
        if obj.get("toolUseResult") is not None:
            return None
        msg = obj.get("message") or {}
        text = _human_text(msg.get("content"))
        if text is None:
            return None
        return ("user", text[:MAX_TEXT_CHARS])
    elif mtype == "assistant":
        if obj.get("isApiErrorMessage"):
            return None
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        text = "\n".join(parts)
        if not text.strip():
            return None
        return ("assistant", text[:MAX_TEXT_CHARS])
    elif mtype == "attachment":
        # A user message queued mid-turn is logged only as a queued_command
        # attachment (there is no separate type:"user" line for it).
        # Background task notifications reuse the same attachment type but
        # carry commandMode "task-notification" and no origin; skip them.
        att = obj.get("attachment") or {}
        if att.get("type") != "queued_command":
            return None
        if att.get("commandMode", "prompt") != "prompt":
            return None
        origin = att.get("origin") or {}
        if origin.get("kind", "human") != "human":
            return None
        text = _human_text(att.get("prompt"))
        if text is None:
            return None
        return ("user", text[:MAX_TEXT_CHARS])
    return None


def _human_text(content):
    """Normalise a user-authored content payload (str or content-block list)
    to plain text. Returns None for empty text or injected payloads (XML-ish
    "<...>" blocks, "Caveat:" notices) that the human did not type."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
        text = "\n".join(parts)
    else:
        return None
    stripped = text.lstrip()
    if stripped.startswith("<") or stripped.startswith("Caveat:"):
        return None
    if not text.strip():
        return None
    return text


def _index_file(conn, file_id, path, start_offset, session_id, project_dir):
    """Incrementally index one jsonl file from start_offset (READ-ONLY, 'rb').

    Returns (messages_added, malformed_lines). Commits periodically, keeping
    files.size_indexed consistent with committed bytes so a crash is resumable.
    Trailing bytes without a final newline are left un-consumed for next sync.
    """
    added = 0
    malformed = 0
    offset = start_offset
    pending_since_commit = 0

    def commit_progress():
        conn.execute(
            "UPDATE files SET size_indexed=? WHERE id=?", (offset, file_id)
        )
        conn.commit()

    with open(path, "rb") as f:
        f.seek(start_offset)
        buf = b""
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = buf[:nl]
                buf = buf[nl + 1:]
                offset += nl + 1  # advance past this complete line
                s = line.decode("utf-8", errors="replace").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except (ValueError, RecursionError):
                    malformed += 1
                    continue
                if not isinstance(obj, dict):
                    malformed += 1
                    continue
                if obj.get("isSidechain"):
                    continue
                ts = obj.get("timestamp")
                if not ts:
                    continue
                extracted = _extract_message(obj)
                if extracted is None:
                    continue
                role, text = extracted
                cur = conn.execute(
                    "INSERT OR IGNORE INTO messages "
                    "(file_id, uuid, session_id, project_dir, cwd, git_branch,"
                    " role, timestamp, text) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        file_id,
                        obj.get("uuid"),
                        obj.get("sessionId") or session_id,
                        project_dir,
                        obj.get("cwd"),
                        obj.get("gitBranch"),
                        role,
                        ts,
                        text,
                    ),
                )
                if cur.rowcount:
                    added += 1
                pending_since_commit += 1
                if pending_since_commit >= COMMIT_EVERY_MESSAGES:
                    commit_progress()
                    pending_since_commit = 0
        # buf now holds trailing bytes with no final newline (possibly a
        # half-written line): do NOT process, do NOT advance offset.
    commit_progress()
    return added, malformed


def sync(conn, projects_dir=None, full=False):
    """Scan projects_dir and bring the index up to date. Never raises for
    per-file problems; they are collected in the returned 'errors' list."""
    t0 = time.time()
    if projects_dir is None:
        projects_dir = Path.home() / ".claude" / "projects"
    projects_dir = Path(projects_dir)

    result = {
        "files_scanned": 0,
        "files_updated": 0,
        "files_removed": 0,
        "messages_added": 0,
        "malformed_lines": 0,
        "errors": [],
        "elapsed_s": 0.0,
    }

    row = conn.execute(
        "SELECT value FROM meta WHERE key='index_version'"
    ).fetchone()
    if row is None or row[0] != str(INDEX_VERSION):
        full = True
        result["rebuilt_for_version"] = INDEX_VERSION

    if full:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM files")
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('index_version', ?)",
            (str(INDEX_VERSION),),
        )
        conn.commit()

    # Discover projects/*/*.jsonl (depth exactly 2; skip subdirectories).
    found = {}  # abs path str -> (stat, project_dir_name)
    try:
        with os.scandir(projects_dir) as it:
            proj_entries = [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError as e:
        result["errors"].append("scan {}: {}".format(projects_dir, e))
        result["elapsed_s"] = round(time.time() - t0, 3)
        return result

    for proj in proj_entries:
        try:
            with os.scandir(proj.path) as it:
                for entry in it:
                    if not entry.name.endswith(".jsonl"):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        result["errors"].append(
                            "stat {}: {}".format(entry.path, e)
                        )
                        continue
                    found[os.path.abspath(entry.path)] = (st, proj.name)
        except OSError as e:
            result["errors"].append("scan {}: {}".format(proj.path, e))

    result["files_scanned"] = len(found)

    # Remove DB entries for files gone from disk.
    db_files = {
        row[1]: row
        for row in conn.execute(
            "SELECT id, path, size_indexed, mtime, inode FROM files"
        )
    }
    for path, row in db_files.items():
        if path not in found:
            conn.execute("DELETE FROM messages WHERE file_id=?", (row[0],))
            conn.execute("DELETE FROM files WHERE id=?", (row[0],))
            result["files_removed"] += 1
    if result["files_removed"]:
        conn.commit()

    for path in sorted(found):
        st, project_dir = found[path]
        session_id = Path(path).stem
        try:
            row = db_files.get(path)
            if row is None:
                cur = conn.execute(
                    "INSERT INTO files (path, size_indexed, mtime, inode,"
                    " session_id, project_dir) VALUES (?,0,?,?,?,?)",
                    (path, st.st_mtime, st.st_ino, session_id, project_dir),
                )
                file_id = cur.lastrowid
                start = 0
            else:
                file_id, _, size_indexed, mtime, inode = row
                if inode != st.st_ino or st.st_size < size_indexed:
                    # Rotated/replaced or truncated: reindex from scratch.
                    conn.execute(
                        "DELETE FROM messages WHERE file_id=?", (file_id,)
                    )
                    conn.execute(
                        "UPDATE files SET size_indexed=0, inode=?, mtime=?"
                        " WHERE id=?",
                        (st.st_ino, st.st_mtime, file_id),
                    )
                    conn.commit()
                    start = 0
                elif st.st_size > size_indexed:
                    start = size_indexed
                    conn.execute(
                        "UPDATE files SET mtime=? WHERE id=?",
                        (st.st_mtime, file_id),
                    )
                else:
                    if mtime != st.st_mtime:
                        conn.execute(
                            "UPDATE files SET mtime=? WHERE id=?",
                            (st.st_mtime, file_id),
                        )
                        conn.commit()
                    continue
            added, malformed = _index_file(
                conn, file_id, path, start, session_id, project_dir
            )
            conn.execute(
                "UPDATE files SET mtime=? WHERE id=?", (st.st_mtime, file_id)
            )
            conn.commit()
            result["files_updated"] += 1
            result["messages_added"] += added
            result["malformed_lines"] += malformed
        except Exception as e:  # per-file safety net
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            result["errors"].append("{}: {}".format(path, e))

    result["elapsed_s"] = round(time.time() - t0, 3)
    return result


def stats(conn):
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    by_role = dict(
        conn.execute("SELECT role, COUNT(*) FROM messages GROUP BY role")
    )
    projects = conn.execute(
        "SELECT COUNT(DISTINCT project_dir) FROM messages"
    ).fetchone()[0]
    first_ts, last_ts = conn.execute(
        "SELECT MIN(timestamp), MAX(timestamp) FROM messages"
    ).fetchone()
    dbp = db_path()
    try:
        db_bytes = os.path.getsize(dbp)
    except OSError:
        db_bytes = 0
    return {
        "files": files,
        "messages": messages,
        "by_role": by_role,
        "projects": projects,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "db_bytes": db_bytes,
        "db_path": str(dbp),
    }


def _main(argv):
    if len(argv) < 2 or argv[1] not in ("index", "stats"):
        print("usage: scope_indexer.py index [--full] | stats", file=sys.stderr)
        return 2
    conn = open_db()
    try:
        if argv[1] == "index":
            full = "--full" in argv[2:]
            res = sync(conn, full=full)
            print(json.dumps(res, indent=2))
            return 1 if res["errors"] else 0
        else:
            print(json.dumps(stats(conn), indent=2))
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
