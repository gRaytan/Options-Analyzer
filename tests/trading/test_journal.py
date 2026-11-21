"""Tests for the append-only journal (plan T04)."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.domain import (
    Action,
    Fill,
    HaltScope,
    HaltState,
    IntentKind,
    OptionContract,
    OrderLeg,
    Right,
    TradeIntent,
)
from src.trading.journal import MIGRATIONS, TABLES, Journal, JournalError

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)


@pytest.fixture
def journal(tmp_path):
    with Journal(tmp_path / "journal.db") as j:
        yield j


def intent(i="i1"):
    return TradeIntent(
        i, "index_put", IntentKind.OPEN, (OrderLeg(PUT, Action.SELL, 1),), "SCAN", T0
    )


def test_all_tables_created(journal):
    assert journal.schema_version == len(MIGRATIONS)
    for table in TABLES:
        assert journal.count(table) == 0


def test_append_and_read_roundtrip(journal):
    journal.append("intents", intent(), key="i1", ts=T0)
    [rec] = journal.read("intents")
    assert rec.data == intent()
    assert rec.key == "i1"
    assert rec.ts == T0


def test_filters_and_ordering(journal):
    for n in range(5):
        journal.append(
            "fills",
            Fill(f"e{n}", "ref-a" if n % 2 else "ref-b", PUT, Action.SELL, 1, 1.0, T0),
            key="ref-a" if n % 2 else "ref-b",
            ts=T0 + timedelta(minutes=n),
        )
    assert [r.data.exec_id for r in journal.read("fills")] == [
        "e0",
        "e1",
        "e2",
        "e3",
        "e4",
    ]
    assert [r.data.exec_id for r in journal.read("fills", key="ref-a")] == ["e1", "e3"]
    since = journal.read("fills", since=T0 + timedelta(minutes=3))
    assert [r.data.exec_id for r in since] == ["e3", "e4"]
    until = journal.read("fills", until=T0 + timedelta(minutes=2))
    assert [r.data.exec_id for r in until] == ["e0", "e1"]
    assert len(journal.read("fills", limit=2)) == 2


def test_latest_gives_current_state(journal):
    journal.append("halts", HaltState(True, HaltScope.ALL, "manual"), key="halt")
    journal.append("halts", HaltState(False), key="halt")
    assert journal.latest("halts").data.active is False
    assert journal.latest("orders") is None


@pytest.mark.parametrize(
    "sql",
    ["UPDATE intents SET key = 'x'", "DELETE FROM intents"],
)
def test_append_only_enforced(tmp_path, sql):
    path = tmp_path / "journal.db"
    with Journal(path) as j:
        j.append("intents", intent(), key="i1")
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    conn.close()


def test_unknown_table_rejected(journal):
    with pytest.raises(JournalError):
        journal.append("intents; DROP TABLE fills", intent())
    with pytest.raises(JournalError):
        journal.read("nope")


def test_reopen_keeps_data_and_does_not_remigrate(tmp_path):
    path = tmp_path / "journal.db"
    with Journal(path) as j:
        j.append("intents", intent(), key="i1")
    with Journal(path) as j:
        assert j.count("intents") == 1
        assert j.schema_version == len(MIGRATIONS)


def test_newer_schema_refused(tmp_path):
    path = tmp_path / "journal.db"
    Journal(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO schema_version VALUES (?)", (len(MIGRATIONS) + 1,))
    conn.commit()
    conn.close()
    with pytest.raises(JournalError, match="newer"):
        Journal(path)


def test_journal_file_is_gitignored():
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/trading/journal.db"], check=False
    )
    assert result.returncode == 0
