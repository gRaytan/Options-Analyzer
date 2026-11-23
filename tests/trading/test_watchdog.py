"""Watchdog and nightly backups (plan T39)."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.calendars import MarketCalendar
from src.trading.config import AlertsConfig, TradingConfig
from src.trading.journal import Journal
from src.trading.notifier import Notifier
from src.trading.scheduler import ET
from src.trading.status import write_heartbeat
from src.trading.watchdog import Watchdog

MARKET = datetime(2026, 11, 23, 11, 0, tzinfo=ET)  # Monday, market open
NIGHT = datetime(2026, 11, 23, 22, 0, tzinfo=ET)
CAL = MarketCalendar()


class Sink:
    name = "sink"

    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


def dog(tmp_path, clock):
    sink = Sink()
    notifier = Notifier(AlertsConfig(min_level="info"), channels=[sink], clock=clock)
    w = Watchdog(
        tmp_path / "hb.json",
        notifier,
        CAL,
        tmp_path / "journal.db",
        tmp_path / "config",
        tmp_path / "backups",
        tmp_path / "watchdog.json",
    )
    return w, sink


def beat(tmp_path, at, **state):
    from src.trading.domain import set_clock

    set_clock(lambda: at)
    try:
        write_heartbeat(
            tmp_path / "hb.json", connected=True, account_stale=False, **state
        )
    finally:
        set_clock(None)


def titles(sink):
    return [(a.level, a.title) for a in sink.sent]


def test_healthy_engine_no_alerts(tmp_path):
    now = MARKET
    w, sink = dog(tmp_path, lambda: now)
    beat(tmp_path, now - timedelta(seconds=50))
    assert w.check(now)["down"] is False and sink.sent == []


def test_killing_the_engine_triggers_an_alert_and_recovery(tmp_path):
    """The plan's done-when: the engine stops, the watchdog alerts."""
    now = MARKET
    w, sink = dog(tmp_path, lambda: now)
    beat(tmp_path, now - timedelta(minutes=4))  # engine died 4 min ago
    assert w.check(now)["down"] is True
    assert titles(sink) == [("critical", "Engine down")]
    w.check(now + timedelta(minutes=1))  # repeat limited (15 min)
    assert len(sink.sent) == 1
    beat(tmp_path, now + timedelta(minutes=2))
    w.check(now + timedelta(minutes=2))
    assert titles(sink)[-1] == ("info", "Engine back")


def test_off_hours_are_more_patient(tmp_path):
    now = NIGHT
    w, sink = dog(tmp_path, lambda: now)
    beat(tmp_path, now - timedelta(minutes=10))
    assert w.check(now)["down"] is False
    beat(tmp_path, now - timedelta(minutes=40))
    w.check(now)
    assert titles(sink) == [("warning", "Engine down")]


def test_never_started(tmp_path):
    now = MARKET
    w, sink = dog(tmp_path, lambda: now)
    w.check(now)
    assert "never started" in sink.sent[0].message


def test_ib_disconnected_for_ten_minutes(tmp_path):
    t = [MARKET]
    w, sink = dog(tmp_path, lambda: t[0])
    for minute in range(12):
        t[0] = MARKET + timedelta(minutes=minute)
        from src.trading.domain import set_clock

        set_clock(lambda: t[0])
        try:
            write_heartbeat(
                tmp_path / "hb.json", connected=False, last_error="2FA needed"
            )
        finally:
            set_clock(None)
        w.check(t[0])
    assert titles(sink) == [("critical", "IB disconnected")]
    assert "2FA needed" in sink.sent[0].message


def test_nightly_backup_and_pruning(tmp_path):
    j = Journal(tmp_path / "journal.db")
    j.append("halts", {"x": 1})
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "trading.json").write_text("{}")
    now = datetime(2026, 11, 24, 2, 5, tzinfo=ET)
    w, _ = dog(tmp_path, lambda: now)
    beat(tmp_path, now)
    for k in range(16):  # old backups: only 14 are kept
        (tmp_path / "backups" / f"2026-10-{k + 1:02d}").mkdir(parents=True)
    result = w.tick(now)
    target = tmp_path / "backups" / "2026-11-24"
    assert result["backup"] == str(target)
    conn = sqlite3.connect(str(target / "journal.db"))
    assert conn.execute("SELECT COUNT(*) FROM halts").fetchone()[0] == 1
    conn.close()
    assert (target / "config" / "trading.json").exists()
    assert len(list((tmp_path / "backups").iterdir())) == 14
    assert "backup" not in w.tick(now + timedelta(minutes=1))  # once a day
    assert json.loads((tmp_path / "watchdog.json").read_text())["down"] is False
    j.close()


def test_engine_keeps_heartbeat_fresh_when_idle(tmp_path, monkeypatch):
    """Off-hours ticks with nothing due still refresh the heartbeat (T38 fix)."""
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    writes = []
    real = run._heartbeat  # pylint: disable=protected-access

    def spy(adapter, **kw):
        writes.append(kw)
        real(adapter, **kw)

    monkeypatch.setattr(run, "_heartbeat", spy)

    class NothingDue:
        def __init__(self, _cal):
            self.calls = 0

        def due(self, now):
            self.calls += 1
            return set() if self.calls > 1 else {"margin"}

        def mark(self, jobs, now):
            pass

    monkeypatch.setattr(run.sched, "Scheduler", NothingDue)
    broker = FakeAdapter(account_id="DU1")

    async def go():
        task = asyncio.create_task(
            run.monitor(
                0.01,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
                config=TradingConfig(),
            )
        )
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert len(writes) >= 3  # the cycle, then idle ticks
    assert "margin" in writes[-1]  # idle ticks keep the last cycle's state
    assert hb.exists()


def test_daily_report_level_always_delivered():
    sink = Sink()
    n = Notifier(AlertsConfig(min_level="critical"), channels=[sink])
    n.notify("report", "daily:2026-11-23", "Daily report", "...")
    n.notify("report", "daily:2026-11-23", "Daily report", "...")  # once per key
    n.notify("info", "x", "Info", "...")
    assert [a.title for a in sink.sent] == ["Daily report"]
