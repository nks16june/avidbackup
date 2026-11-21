from __future__ import annotations

import sqlite3
from pathlib import Path

from avidbackup.history import HistoryStore


def test_database_with_an_extra_legacy_column_still_works(tmp_path: Path):
    db = tmp_path / "history.sqlite3"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
            files_downloaded INTEGER DEFAULT 0, files_skipped INTEGER DEFAULT 0,
            files_failed INTEGER DEFAULT 0, files_bytes INTEGER DEFAULT 0,
            mysql_downloaded INTEGER DEFAULT 0, mysql_bytes INTEGER DEFAULT 0,
            mysql_path TEXT, mysql_config_source TEXT, notify_sent INTEGER DEFAULT 0,
            whatsapp_sent INTEGER DEFAULT 0, error TEXT, detail_json TEXT
        );
        INSERT INTO runs (started_at, status) VALUES ('2026-11-01T01:00:00Z', 'success');
        """
    )
    conn.commit()
    conn.close()

    store = HistoryStore(db)
    run_id = store.start_run()
    store.finish_run(run_id, status="success", files_downloaded=3)
    runs = store.list_runs()
    assert len(runs) == 2 and runs[0]["files_downloaded"] == 3


def test_fresh_database_has_no_legacy_columns(tmp_path: Path):
    db = tmp_path / "history.sqlite3"
    HistoryStore(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(runs)")}
    assert "notify_sent" in cols and "whatsapp_sent" not in cols
