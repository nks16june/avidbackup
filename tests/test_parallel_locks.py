from __future__ import annotations

import threading
from pathlib import Path

import pytest

from avidbackup.history import HistoryStore, interrupt_reason
from avidbackup.locks import acquire_run_lock


def test_two_accounts_can_lock_at_once(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    release_a = acquire_run_lock(a, "site-a")
    release_b = acquire_run_lock(b, "site-b")
    release_a()
    release_b()


def test_same_account_blocks_second_thread(tmp_path: Path):
    root = tmp_path / "one"
    release = acquire_run_lock(root, "same")
    with pytest.raises(RuntimeError, match="already has a backup running"):
        acquire_run_lock(root, "same")
    release()
    release2 = acquire_run_lock(root, "same")
    release2()


def test_close_stale_marks_dead_pid(tmp_path: Path):
    hist = HistoryStore(tmp_path / "history.sqlite3")
    run_id = hist.start_run(account_id="demo", detail={"pid": 1})  # PID 1 is usually gone on Windows
    # Force a clearly dead pid
    with hist._conn() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE runs SET detail_json=? WHERE id=?",
            ('{"pid": 99999991, "account_id": "demo"}', run_id),
        )
    closed = hist.close_stale_runs(account_id="demo")
    assert closed == 1
    row = hist.latest_run()
    assert row is not None
    assert row["status"] == "interrupted"
    assert "99999991" in (row["error"] or "")
    assert "Task Scheduler" in (row["error"] or "")


def test_interrupt_reason_text():
    text = interrupt_reason(pid=42, account_id="site.example.com")
    assert "site.example.com" in text
    assert "42" in text


def test_parallel_lock_release_from_threads(tmp_path: Path):
    errors: list[str] = []

    def worker(name: str) -> None:
        try:
            release = acquire_run_lock(tmp_path / name, name)
            release()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    threads = [threading.Thread(target=worker, args=(f"acct{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
