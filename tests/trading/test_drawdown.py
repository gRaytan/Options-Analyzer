"""Options-book drawdown protection: halve at 5%, stop at 8%, normal under 2%."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    Bar,
    ExerciseStyle,
    Fill,
    HistoryKind,
    OptionContract,
    Position,
    Quote,
    Right,
    set_clock,
)
from src.trading.journal import Journal
from src.trading.risk.budget import BudgetTracker

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig()
PUT = OptionContract(
    "XSP", date(2027, 1, 15), 650, Right.PUT, 100, ExerciseStyle.EUROPEAN, True, "XSP"
)


def broker_with(mark):
    b = FakeAdapter(
        account_id="DU1",
        account=AccountSnapshot(
            "DU1", NOW, 500_000, 350_000, 150_000, 150_000, 350_000
        ),
    )
    b.set_history(
        "VIX",
        HistoryKind.PRICE,
        [Bar(TODAY - timedelta(days=5 - i), 18, 18, 18, 18) for i in range(6)],
    )
    b.set_position(PUT, -10, 5.0)
    b.set_quote(Quote(PUT, mark - 0.05, mark + 0.05, NOW, underlying_price=690.0))
    asyncio.run(b.connect())
    return b


def journal_with_sale(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.append("fills", Fill("e1", "o1", PUT, Action.SELL, 10, 5.0, NOW, 10.0), key="o1")
    return j


def update(tracker, broker):
    snap = asyncio.run(broker.account_snapshot())
    return asyncio.run(tracker.update(broker, asyncio.run(broker.positions()), snap))


@pytest.fixture(autouse=True)
def clock():
    set_clock(lambda: NOW)
    yield
    set_clock(None)


def test_options_pnl_peak_and_drawdown_states(tmp_path):
    j = journal_with_sale(tmp_path)
    t = BudgetTracker(CFG, j)
    # Sold 10 puts at $5.00 ($5,000 - $10 fees); marked at $1.00: P&L $3,990 (the peak).
    r = update(t, broker_with(1.0))
    assert r["options_pnl"] == pytest.approx(3_990) and r["drawdown"] == 0
    assert not r["reduced"] and r["multiplier"] == pytest.approx(0.8)  # VIX 18
    # Marked at $26: P&L -$21,010 -> $25,000 below the peak = 5% of $500k: halved.
    r = update(t, broker_with(26.0))
    assert r["drawdown"] == pytest.approx(0.05) and r["reduced"] and not r["halted"]
    assert r["multiplier"] == pytest.approx(0.4)
    # Marked at $41: $40,000 below the peak = 8%: no new risk.
    r = update(t, broker_with(41.0))
    assert r["halted"] and r["multiplier"] == 0 and r["free"] == 0
    # Back to $12: 2.2% -> still halved until under 2%; at $9: 1.6% -> normal.
    r = update(t, broker_with(12.0))
    assert r["drawdown"] == pytest.approx(0.022) and r["reduced"] and not r["halted"]
    r = update(t, broker_with(9.0))
    assert not r["reduced"] and r["multiplier"] == pytest.approx(0.8)


def test_peak_survives_a_restart(tmp_path):
    j = journal_with_sale(tmp_path)
    update(BudgetTracker(CFG, j), broker_with(1.0))
    fresh = BudgetTracker(CFG, Journal(tmp_path / "j.db"))
    r = update(fresh, broker_with(26.0))
    assert r["options_peak"] == pytest.approx(3_990) and r["reduced"]


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, level, key, title, message):
        self.sent.append((level, title))


def test_alerts_on_each_change():
    n = Notes()
    normal = {"reduced": False, "halted": False, "drawdown": 0.0}
    reduced = {"reduced": True, "halted": False, "drawdown": 0.055}
    halted = {"reduced": True, "halted": True, "drawdown": 0.081}
    run._drawdown_alert(n, normal, reduced, CFG)  # pylint: disable=protected-access
    run._drawdown_alert(n, reduced, reduced, CFG)  # pylint: disable=protected-access
    run._drawdown_alert(n, reduced, halted, CFG)  # pylint: disable=protected-access
    run._drawdown_alert(
        n, halted, {**normal, "drawdown": 0.01}, CFG
    )  # pylint: disable=protected-access
    run._drawdown_alert(
        n, {}, normal, CFG
    )  # pylint: disable=protected-access  # start: quiet
    assert n.sent == [
        ("warning", "Options drawdown 5.5%: risk halved"),
        ("critical", "Options drawdown 8.1%: no new trades"),
        ("info", "Options drawdown recovered"),
    ]


def test_status_page_shows_the_drawdown(tmp_path, monkeypatch):
    from src.trading.status import write_heartbeat
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(tmp_path / "hb.json"))
    budget = {
        "regime": "normal",
        "vix": 18,
        "multiplier": 0.4,
        "spreads_only": False,
        "budget": 20_000,
        "used": 5_000,
        "free": 15_000,
        "over_budget": False,
        "regime_detail": "VIX 18",
        "drawdown": 0.055,
        "options_pnl": -24_000,
        "options_peak": 3_500,
        "reduced": True,
        "halted": False,
    }
    write_heartbeat(tmp_path / "hb.json", connected=True, budget=budget)
    html = create_app().test_client().get("/bot").get_data(as_text=True)
    assert "Options drawdown: <b>5.5%</b>" in html and "risk halved" in html
