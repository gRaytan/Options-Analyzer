"""Scheduler on the NYSE calendar (plan T38, spec §8)."""
import asyncio
from collections import defaultdict
from datetime import date, datetime, time, timedelta

import pytest

from src.trading import run
from src.trading import scheduler as sched
from src.trading.broker.fake import FakeAdapter
from src.trading.calendars import MarketCalendar
from src.trading.config import TradingConfig
from src.trading.domain import (
    Action,
    Fill,
    OptionContract,
    Right,
    StockContract,
    set_clock,
)
from src.trading.journal import Journal
from src.trading.kill_switch import HaltManager
from src.trading.margin_monitor import MarginMonitor
from src.trading.risk.budget import BudgetTracker

ET = sched.ET
CAL = MarketCalendar()


def simulate(day: date, start=time(7, 0), end=time(18, 0)):
    """Tick every minute; return job -> list of ET times it ran."""
    s = sched.Scheduler(CAL)
    t = datetime.combine(day, start, tzinfo=ET)
    stop = datetime.combine(day, end, tzinfo=ET)
    ran = defaultdict(list)
    while t <= stop:
        jobs = s.due(t)
        s.mark(jobs, t)
        for j in jobs:
            ran[j].append(t.time())
        t += timedelta(minutes=1)
    return ran


def test_normal_day():
    ran = simulate(date(2026, 11, 23))  # Monday
    assert ran[sched.PREOPEN] == [time(9, 0)]
    assert ran[sched.MANAGE][0] == time(9, 45) and ran[sched.MANAGE][-1] == time(15, 45)
    assert len(ran[sched.MANAGE]) == 73  # every 5 min
    assert ran[sched.SCAN][:3] == [time(9, 45), time(10, 0), time(10, 15)]
    assert len(ran[sched.SCAN]) == 25 and ran[sched.SCAN][-1] == time(15, 45)
    assert ran[sched.EXPIRY_SWEEP] == [time(15, 0)]
    assert ran[sched.EOD] == [time(16, 15)]
    margin = ran[sched.MARGIN]
    in_hours = [t for t in margin if time(9, 31) <= t < time(16, 0)]
    assert len(in_hours) == 389  # every minute 09:31-15:59
    assert time(8, 0) in margin and time(8, 1) not in margin  # 15 min when closed


def test_early_close_pulls_jobs_forward():
    ran = simulate(date(2026, 11, 27))  # day after Thanksgiving: 13:00 close
    assert ran[sched.EXPIRY_SWEEP] == [time(12, 0)]
    assert ran[sched.EOD] == [time(13, 15)]
    assert ran[sched.MANAGE][-1] == time(12, 45)


def test_holiday_only_watches_the_account():
    ran = simulate(date(2026, 11, 26))  # Thanksgiving
    assert set(ran) == {sched.MARGIN}
    assert len(ran[sched.MARGIN]) == 45  # every 15 min, 07:00-18:00


def test_late_start_still_runs_preopen_once():
    s = sched.Scheduler(CAL)
    t = datetime(2026, 11, 23, 11, 7, tzinfo=ET)
    jobs = s.due(t)
    assert sched.PREOPEN in jobs and sched.MANAGE in jobs and sched.SCAN in jobs
    s.mark(jobs, t)
    assert sched.PREOPEN not in s.due(t + timedelta(minutes=5))


# -- a dry-run day through the engine ---------------------------------------------------


@pytest.fixture
def engine(tmp_path, monkeypatch):
    acted = []

    async def fake_act(
        executor,
        journal,
        config,
        margin,
        positions,
        snap,
        notifier,
        entry_ctx=None,
        rebuild_ctx=None,
    ):
        acted.append((run.utcnow().astimezone(ET).time(), entry_ctx is not None))

    monkeypatch.setattr(run, "_act", fake_act)
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    broker = FakeAdapter(account_id="DU1", read_only=False, clock=lambda: run.utcnow())
    broker.set_position(StockContract("SPY"), 400, 670.0)
    expiring = OptionContract("SPY", date(2026, 11, 23), 700, Right.CALL)
    broker.set_position(expiring, -1, 0.5)
    yield broker, acted, tmp_path
    set_clock(None)


def test_dry_run_day_job_timing(engine):
    broker, acted, tmp = engine
    journal = Journal(tmp / "j.db")
    config = TradingConfig()
    halts = HaltManager(journal, tmp / "H")
    margin = MarginMonitor(config.margin_floor)
    budget = BudgetTracker(config, journal)
    alerts = []

    class Notes:
        def notify(self, level, key, title, message):
            alerts.append((level, title, message))

        async def anotify(self, *a):
            self.notify(*a)

    s = sched.Scheduler(CAL)
    asyncio.run(broker.connect())
    t = datetime(2026, 11, 23, 8, 50, tzinfo=ET)
    observed = 0
    state = ({}, None)
    while t <= datetime(2026, 11, 23, 16, 30, tzinfo=ET):
        now = t
        set_clock(lambda: now)
        jobs = s.due(now)
        if jobs:
            entry = run.EntryContext(halts, budget, None, journal)
            state = asyncio.run(
                run._cycle(  # pylint: disable=protected-access
                    broker,
                    journal,
                    halts,
                    margin,
                    state[1],
                    state[0],
                    budget,
                    Notes(),
                    None,
                    config,
                    object(),
                    entry,
                    jobs=jobs,
                )
            )
            s.mark(jobs, now)
            observed += bool(jobs - {sched.MARGIN})
        t += timedelta(minutes=1)

    # Acting (management) every 5 minutes 09:45-15:45; entries only on scan ticks.
    times = [a[0] for a in acted]
    assert times[0] == time(9, 45) and times[-1] == time(15, 45)
    assert sum(1 for _, scan in acted if scan) == 25
    assert len(acted) == 73  # the 15:00 sweep coincides with a manage tick
    runs = {r.data["job"]: r.data for r in journal.read("job_runs")}
    assert runs["preopen"]["connected"] is True
    assert runs["expiry_sweep"]["expiring"] == ["SPY 2026-11-23 C 700"]
    assert runs["eod"]["date"] == "2026-11-23" and runs["eod"]["open_options"] == 1
    titles = [a[1] for a in alerts]
    assert "Expiring today: 1" in titles and "Daily report 2026-11-23" in titles
    assert observed == 73 + 2  # + pre-open and end of day


def test_daily_report_counts_todays_fills(tmp_path):
    journal = Journal(tmp_path / "j.db")
    now = datetime(2026, 11, 23, 16, 15, tzinfo=ET)
    set_clock(lambda: now)
    try:
        put = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT)
        journal.append(
            "fills", Fill("e1", "o1", put, Action.SELL, 5, 2.0, now, 5.75), key="o1"
        )
        margin = MarginMonitor(TradingConfig().margin_floor)
        rep = run._daily_report(
            journal, None, None, margin, [], None
        )  # pylint: disable=protected-access
    finally:
        set_clock(None)
    assert rep["fills"] == 1 and rep["premium_sold"] == 1000 and rep["fees"] == 5.75
