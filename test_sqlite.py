"""SQLite smoke check.

This script used to open `jarvis_projects.db` - the owner's real deadline
store - and create a table in it, because a file named test_*.py is collected
by pytest and its module level runs at import. It now works on a throwaway
database and leaves the live one alone.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path


def test_sqlite_roundtrip_in_a_temp_database():
    path = Path(tempfile.mkdtemp(prefix="jarvis-sqlite-smoke-")) / "smoke.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS smoke (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO smoke (note) VALUES ('ok')")
        conn.commit()
        assert conn.execute("SELECT note FROM smoke").fetchone()[0] == "ok"
    finally:
        conn.close()
    assert path.exists()


if __name__ == "__main__":
    test_sqlite_roundtrip_in_a_temp_database()
    print("SQLite test: temp-database round trip passed (the live store was not touched)")
