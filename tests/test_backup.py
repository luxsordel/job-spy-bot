"""Бэкапы базы (#20)."""

from __future__ import annotations

import asyncio
import gzip
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobspybot.backup import Backups, backup_loop, run_and_send, seconds_until
from jobspybot.config import ConfigError, Settings
from jobspybot.db import Database


@pytest.fixture
def filedb(tmp_path):
    db = Database(tmp_path / "jobspy.sqlite3")
    db.ensure_user(1, "anna", allowed=True)
    db.add_resume(1, "CV", "Python developer " * 20)
    yield db
    db.close()


async def test_backup_is_consistent_gzip_copy(filedb, tmp_path):
    backups = Backups(filedb, tmp_path / "backups", keep=3)
    info = await backups.make()
    assert info.path.name.endswith(".sqlite3.gz") and info.size > 0
    restored = tmp_path / "restored.sqlite3"
    restored.write_bytes(gzip.decompress(info.path.read_bytes()))
    conn = sqlite3.connect(restored)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "anna"
    assert conn.execute("SELECT COUNT(*) FROM resumes").fetchone()[0] == 1
    conn.close()
    assert not list((tmp_path / "backups").glob("*.tmp"))


async def test_rotation_keeps_newest(filedb, tmp_path):
    backups = Backups(filedb, tmp_path / "b", keep=2)
    start = datetime(2026, 11, 1, 3, tzinfo=UTC)
    for day in range(4):
        await backups.make(start + timedelta(days=day))
    names = [b.path.name for b in backups.copies()]
    assert names == ["jobspy-2026-11-04_030000.sqlite3.gz", "jobspy-2026-11-03_030000.sqlite3.gz"]


async def test_sent_to_admins(filedb, tmp_path):
    sent = []

    async def send(name, data, caption):
        sent.append((name, len(data), caption))

    info = await run_and_send(Backups(filedb, tmp_path / "b"), send)
    assert sent == [(info.path.name, info.size, sent[0][2])] and "Бэкап базы" in sent[0][2]


async def test_loop_backs_up_on_first_start(filedb, tmp_path):
    backups = Backups(filedb, tmp_path / "b")
    stop = asyncio.Event()
    task = asyncio.create_task(backup_loop(backups, None, 3, stop))
    for _ in range(50):
        if backups.copies():
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(backups.copies()) == 1


def test_seconds_until():
    now = datetime(2026, 11, 20, 2, 30, tzinfo=UTC)
    assert seconds_until(3, now) == 1800
    assert seconds_until(2, now) == 23.5 * 3600


def test_settings():
    s = Settings.from_env({"DB_PATH": "/var/lib/x/jobspy.sqlite3"})
    assert s.backup_dir.as_posix() == "/var/lib/x/backups" and s.backup.keep == 7
    assert not Settings.from_env({"BACKUP_TO_TELEGRAM": "0"}).backup.to_telegram
    with pytest.raises(ConfigError):
        Settings.from_env({"BACKUP_HOUR_UTC": "24"})
