"""Job scheduler on the NYSE calendar (plan T38, spec §8).

The engine ticks every 60 s; each tick asks :meth:`Scheduler.due` which jobs
to run. Times follow the day's real session, so holidays have no jobs and
early closes (13:00) pull the afternoon jobs forward.

| Job | When (ET, a normal day) | What runs |
|---|---|---|
| ``preopen`` | once, from 09:00 (30 min before the open) | refresh earnings / ex-dividend calendars, check the connection and permissions, reconcile |
| ``margin`` | every 60 s 09:31-16:00; every 15 min otherwise | account values and the margin monitor; a breach triggers restoration at once |
| ``manage`` | every 5 min 09:45-15:45 | reconcile, guards and rolls, exits, budget, assignment undo |
| ``scan`` | every 15 min 09:45-15:45 | scanner + tracker + entries (on top of ``manage``) |
| ``expiry_sweep`` | once, 15:00 (an hour before an early close) | everything expiring today: one more manage pass, and an alert listing it |
| ``opportunities`` | once, 10:00 (30 min after the open) | the daily opportunities digest: the best candidates from the latest scan |
| ``eod`` | once, 16:15 | reconcile, budget, and the daily report |

The scanner itself runs on every ``scan`` tick as a **report**, whether or
not the bot may open trades (read-only and shadow too), so the Opportunities
page always shows today's real market.

The monthly tail-hedge job (§5.12) is not scheduled: the hedge is on hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Set
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

PREOPEN, MARGIN, MANAGE, SCAN, EXPIRY_SWEEP, EOD, OPPORTUNITIES = (
    "preopen",
    "margin",
    "manage",
    "scan",
    "expiry_sweep",
    "eod",
    "opportunities",
)
ALL_JOBS = frozenset({PREOPEN, MARGIN, MANAGE, SCAN, EXPIRY_SWEEP, EOD, OPPORTUNITIES})
DIGEST_AFTER_OPEN = timedelta(minutes=30)

MARGIN_EVERY = timedelta(seconds=60)
MARGIN_EVERY_CLOSED = timedelta(minutes=15)
MANAGE_EVERY = timedelta(minutes=5)
SCAN_EVERY = timedelta(minutes=15)
PREOPEN_LEAD = timedelta(minutes=30)
MARGIN_START = timedelta(minutes=1)
TRADE_MARGIN = timedelta(minutes=15)  # no manage/scan in the first/last 15 min
EOD_DELAY = timedelta(minutes=15)
SWEEP_AT = time(15, 0)
SWEEP_BEFORE_CLOSE = timedelta(hours=1)
TOLERANCE = timedelta(seconds=2)  # a tick a moment early still counts


@dataclass
class Scheduler:
    """Decides the jobs due at ``now``; remembers what already ran."""

    calendar: Any  # MarketCalendar-like: session(day) -> (open, close) UTC or None
    last_run: Dict[str, datetime] = field(default_factory=dict)
    once_done: Dict[str, date] = field(default_factory=dict)

    def _every(self, job: str, now: datetime, period: timedelta) -> bool:
        last = self.last_run.get(job)
        return last is None or now - last >= period - TOLERANCE

    def _once(self, job: str, day: date) -> bool:
        return self.once_done.get(job) != day

    def sweep_time(self, day: date, close: datetime) -> datetime:
        at = datetime.combine(day, SWEEP_AT, tzinfo=ET)
        return min(at, close - SWEEP_BEFORE_CLOSE)

    def due(self, now: datetime) -> Set[str]:
        day = now.astimezone(ET).date()
        session = self.calendar.session(day)
        jobs: Set[str] = set()
        if session is None:  # weekend / holiday: just watch the account
            if self._every(MARGIN, now, MARGIN_EVERY_CLOSED):
                jobs.add(MARGIN)
            return jobs
        open_, close = session
        market_open = open_ + MARGIN_START <= now < close
        if self._every(
            MARGIN, now, MARGIN_EVERY if market_open else MARGIN_EVERY_CLOSED
        ):
            jobs.add(MARGIN)
        if now >= open_ - PREOPEN_LEAD and now < close and self._once(PREOPEN, day):
            jobs.add(PREOPEN)
        trading = open_ + TRADE_MARGIN <= now <= close - TRADE_MARGIN
        if trading and self._every(MANAGE, now, MANAGE_EVERY):
            jobs.add(MANAGE)
        if trading and self._every(SCAN, now, SCAN_EVERY):
            jobs.add(SCAN)
        if self.sweep_time(day, close) <= now < close and self._once(EXPIRY_SWEEP, day):
            jobs.add(EXPIRY_SWEEP)
        if open_ + DIGEST_AFTER_OPEN <= now < close and self._once(OPPORTUNITIES, day):
            jobs.add(OPPORTUNITIES)
        if now >= close + EOD_DELAY and self._once(EOD, day):
            jobs.add(EOD)
        return jobs

    def mark(self, jobs: Set[str], now: datetime) -> None:
        day = now.astimezone(ET).date()
        for job in jobs:
            self.last_run[job] = now
            if job in (PREOPEN, EXPIRY_SWEEP, EOD, OPPORTUNITIES):
                self.once_done[job] = day
