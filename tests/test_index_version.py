"""An index built by an older indexer (different INDEX_VERSION, or none
recorded) is rebuilt once on the next sync, so stale rows produced by
old extraction rules do not linger. Matching versions sync incrementally."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scope_indexer  # noqa: E402


def _mem_db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(scope_indexer.SCHEMA)
    return conn


def _write_session(projects_dir, lines):
    proj = Path(projects_dir) / "-Users-me-proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / "abc.jsonl"
    with open(path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")
    return path


USER_LINE = {
    "type": "user",
    "uuid": "u1",
    "sessionId": "abc",
    "timestamp": "2026-08-20T00:00:00.000Z",
    "message": {"role": "user", "content": "hello from a real human"},
}


class IndexVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self.tmp.name)
        self.session = _write_session(self.projects, [USER_LINE])

    def tearDown(self):
        self.tmp.cleanup()

    def _plant_stale_row(self, conn):
        # Simulate an index built by an older indexer: the session file is
        # recorded as fully consumed (so incremental sync skips it), and it
        # contributed a row that current extraction rules would reject.
        st = self.session.stat()
        conn.execute(
            "INSERT INTO files(id, path, size_indexed, mtime, inode)"
            " VALUES (1, ?, ?, ?, ?)",
            (str(self.session), st.st_size, st.st_mtime, st.st_ino),
        )
        conn.execute(
            "INSERT INTO messages(file_id, uuid, session_id, project_dir, role,"
            " timestamp, text) VALUES (1, 'stale', 's', 'p', 'user', 't',"
            " '<task-notification>old noise</task-notification>')"
        )
        conn.commit()

    def test_unversioned_index_is_rebuilt(self):
        conn = _mem_db()
        self._plant_stale_row(conn)
        scope_indexer.sync(conn, projects_dir=self.projects)
        texts = [r[0] for r in conn.execute("SELECT text FROM messages")]
        self.assertEqual(texts, ["hello from a real human"])
        ver = conn.execute(
            "SELECT value FROM meta WHERE key='index_version'"
        ).fetchone()[0]
        self.assertEqual(ver, str(scope_indexer.INDEX_VERSION))

    def test_old_version_is_rebuilt(self):
        conn = _mem_db()
        conn.execute("INSERT INTO meta VALUES ('index_version', '0')")
        self._plant_stale_row(conn)
        scope_indexer.sync(conn, projects_dir=self.projects)
        texts = [r[0] for r in conn.execute("SELECT text FROM messages")]
        self.assertEqual(texts, ["hello from a real human"])

    def test_current_version_syncs_incrementally(self):
        conn = _mem_db()
        scope_indexer.sync(conn, projects_dir=self.projects)
        # Second sync must not re-read or re-insert anything.
        res = scope_indexer.sync(conn, projects_dir=self.projects)
        self.assertEqual(res["messages_added"], 0)
        self.assertEqual(res["files_updated"], 0)
        n = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
