"""Margin floor monitor (plan T16)."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.config import MarginFloorConfig
from src.trading.domain import AccountSnapshot
from src.trading.journal import Journal
from src.trading.margin_monitor import MarginMonitor
from src.trading.status import MarginState

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def snap(el, nlv=1_000_000, ts=T0):
    return AccountSnapshot("DU1", ts, nlv, el, 300_000, 380_000, 420_000)


@pytest.fixture
def mon():
    clock = Clock()
    m = MarginMonitor(MarginFloorConfig(), clock)
    m.clock = clock
    return m


def test_states_from_snapshot(mon):
    assert mon.update(snap(450_000)).state is MarginState.NORMAL
    assert mon.update(snap(350_000)).state is MarginState.WARNING  # cushion 35%
    assert mon.update(snap(250_000)).state is MarginState.BREACH  # cushion 25%
    assert mon.update(snap(140_000)).state is MarginState.EMERGENCY  # cushion 14%


def test_losses_between_refreshes_lower_estimate_gains_ignored(mon):
    s = snap(420_000)
    assert mon.update(s, daily_pnl=-1_000).state is MarginState.NORMAL
    mon.clock.now += timedelta(seconds=60)
    r = mon.update(s, daily_pnl=-31_000)  # $30k loss: 390k / 970k = 40.2%, still normal
    assert r.excess_liquidity == 390_000 and r.ib_excess_liquidity == 420_000
    assert r.state is MarginState.NORMAL
    r = mon.update(s, daily_pnl=-41_000)  # $40k loss: 380k / 960k = 39.6% -> warning
    assert r.state is MarginState.WARNING
    assert r.cushion == pytest.approx(380_000 / 960_000)
    assert "P&L loss" in r.reason
    r = mon.update(s, daily_pnl=+50_000)  # a gain doesn't raise the estimate
    assert r.excess_liquidity == 420_000 and r.pnl_since_refresh == 0


def test_new_ib_refresh_resets_estimate_base(mon):
    mon.update(snap(420_000), daily_pnl=0)
    mon.clock.now += timedelta(seconds=180)
    fresh = snap(380_000, ts=mon.clock.now)  # IB now includes the loss itself
    r = mon.update(fresh, daily_pnl=-40_000)
    assert r.excess_liquidity == 380_000  # not double-counted


def test_stale_data_is_at_least_warning(mon):
    s = snap(450_000)
    assert mon.update(s).state is MarginState.NORMAL
    mon.clock.now += timedelta(seconds=241)
    r = mon.update(s)
    assert r.state is MarginState.WARNING and r.stale and "old" in r.reason


def test_hysteresis_after_breach(mon):
    nlv = 1_000_000
    assert mon.update(snap(280_000, nlv)).state is MarginState.BREACH  # 28%
    # Back above the 30% breach line, but below the 35% restore level: stay breached.
    assert mon.update(snap(320_000, nlv)).state is MarginState.BREACH
    # Restored: >= $110k and >= 35% -> warning (still < 40%), then normal.
    assert mon.update(snap(360_000, nlv)).state is MarginState.WARNING
    assert mon.update(snap(450_000, nlv)).state is MarginState.NORMAL


def test_hysteresis_on_absolute_floor(mon):
    nlv = 200_000  # small account: the $ floors bind
    assert mon.update(snap(95_000, nlv)).state is MarginState.BREACH
    assert mon.update(snap(105_000, nlv)).state is MarginState.BREACH  # < $110k restore
    assert (
        mon.update(snap(112_000, nlv)).state is MarginState.WARNING
    )  # restored, < $125k


def test_emergency_downgrades_to_breach_not_further(mon):
    assert mon.update(snap(140_000)).state is MarginState.EMERGENCY
    assert mon.update(snap(320_000)).state is MarginState.BREACH  # not yet restored


def test_no_data_is_unknown_and_blocks_opening(mon):
    r = mon.update(None)
    assert r.state is MarginState.UNKNOWN and not r.allows_opening


def test_listener_only_on_change(mon):
    changes = []
    mon.on_change(
        lambda new, old: changes.append((old.state if old else None, new.state))
    )
    mon.update(snap(450_000))
    mon.update(snap(440_000))
    mon.update(snap(250_000))
    assert changes == [
        (None, MarginState.NORMAL),
        (MarginState.NORMAL, MarginState.BREACH),
    ]


def test_engine_journals_margin_events_and_heartbeat(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = FakeAdapter(account_id="DU123", read_only=True)
    broker.set_account(excess_liquidity=95_000, net_liquidation=200_000)
    broker.pnl = -2_000
    journal = Journal(tmp_path / "j.db")
    asyncio.run(
        run.monitor(
            0, once=True, adapter=broker, journal=journal, halt_file=tmp_path / "HALT"
        )
    )
    beat = json.loads(hb.read_text())
    assert beat["margin"]["state"] == "breach"
    events = Journal(tmp_path / "j.db").read("margin_events")
    assert [e.data["state"] for e in events] == ["breach"]


def test_ib_adapter_daily_pnl():
    from src.trading.broker.ib_adapter import IBAdapter
    from tests.trading.test_ib_adapter import FakeIB, no_sleep

    ib = FakeIB()
    adapter = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)
    assert adapter.daily_pnl() is None
    asyncio.run(adapter.connect())
    assert adapter.daily_pnl() is None  # NaN until IB sends a value
    ib.pnl.dailyPnL = -1234.5
    assert adapter.daily_pnl() == -1234.5
