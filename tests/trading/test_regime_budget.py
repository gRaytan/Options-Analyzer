"""Volatility regime and live risk budget (plan T19b, spec §6.6)."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.config import RegimeConfig, RiskConfig, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    ExerciseStyle,
    Greeks,
    HistoryKind,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
)
from src.trading.journal import Journal
from src.trading.risk.budget import BudgetTracker
from src.trading.risk.regime import Regime, budget_state, classify_regime, vix_inputs

CFG = RegimeConfig()
RISK = RiskConfig()
NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "vix,recent,name,mult",
    [
        (11.5, [12, 12, 12, 12, 12], "low", 0.5),
        (16, [15, 15, 16, 16, 17], "normal", 0.8),
        (24, [20, 22, 23, 25, 24], "elevated", 1.0),
        (30, [25, 26, 27, 28, 29], "elevated", 1.0),
        (38, [25, 28, 31, 34, 36], "rising", 0.0),  # above 5-day average
        (34, [30, 45, 42, 40, 38], "fading", 0.6),  # 24% below the 45 high
        (
            40,
            [38, 44, 43, 42, 41],
            "rising",
            0.0,
        ),  # below average, only 9% off the high
        (None, [], "unknown", 0.0),
    ],
)
def test_classify_regime(vix, recent, name, mult):
    r = classify_regime(vix, recent, CFG)
    assert (r.name, r.multiplier) == (name, mult)


def test_fading_is_spreads_only_low_delta():
    r = classify_regime(34, [30, 45, 42, 40, 38], CFG)
    assert r.spreads_only and r.max_delta == 0.10
    assert classify_regime(11, [12] * 5, CFG).max_delta == 0.10


def test_vix_inputs():
    assert vix_inputs([10, 11, 12, 13, 14, 15, 16]) == (16, [11, 12, 13, 14, 15])
    assert vix_inputs([]) == (None, [])


def test_drawdown_halving_with_hysteresis_and_halt():
    normal = Regime("elevated", 1.0)
    s = budget_state(normal, 0.03, False, RISK)
    assert not s.reduced and s.multiplier == 1.0
    s = budget_state(normal, 0.05, False, RISK)
    assert s.reduced and s.multiplier == 0.5
    s = budget_state(normal, 0.03, True, RISK)  # recovering, still > 2%
    assert s.reduced and s.multiplier == 0.5
    s = budget_state(normal, 0.019, True, RISK)  # recovered to within 2%
    assert not s.reduced and s.multiplier == 1.0
    s = budget_state(normal, 0.08, False, RISK)
    assert s.halted and s.multiplier == 0.0
    assert budget_state(normal, 0.0, False, RISK).budget(1_000_000, RISK) == 100_000


XSP = OptionContract(
    "XSP",
    date(2026, 12, 31),
    600,
    Right.PUT,
    style=ExerciseStyle.EUROPEAN,
    cash_settled=True,
)


def vix_bars(values):
    start = date(2026, 11, 13)
    return [Bar(start + timedelta(days=i), v, v, v, v) for i, v in enumerate(values)]


def broker_with(vix_values):
    b = FakeAdapter(account_id="DU1", read_only=True)
    asyncio.run(b.connect())
    b.set_history("VIX", HistoryKind.PRICE, vix_bars(vix_values))
    b.set_quote(
        Quote(XSP, 0.6, 0.7, NOW, underlying_price=660, greeks=Greeks(-0.05, iv=0.18))
    )
    return b


ACCOUNT = AccountSnapshot("DU1", NOW, 1_000_000, 700_000, 300_000, 300_000, 650_000)


def test_budget_tracker_measures_current_book(tmp_path):
    broker = broker_with([22, 23, 24, 25, 24, 23])
    tracker = BudgetTracker(
        TradingConfig(), Journal(tmp_path / "j.db"), clock=lambda: NOW
    )
    positions = [
        Position(StockContract("SPY"), 400, 520.0, 670.0),
        Position(XSP, -10, 0.65),
    ]
    result = asyncio.run(tracker.update(broker, positions, ACCOUNT))
    assert result["regime"] == "elevated" and result["budget"] == 100_000
    assert 65_000 < result["used"] < 75_000  # 10 x ~$7.06k
    assert not result["over_budget"] and result["free"] == pytest.approx(
        100_000 - result["used"]
    )
    assert Journal(tmp_path / "j.db").count("budget_snapshot") == 1


def test_budget_tracker_rising_vix_means_no_budget(tmp_path):
    broker = broker_with([25, 28, 31, 34, 36, 38])
    tracker = BudgetTracker(
        TradingConfig(), Journal(tmp_path / "j.db"), clock=lambda: NOW
    )
    result = asyncio.run(tracker.update(broker, [Position(XSP, -1, 0.65)], ACCOUNT))
    assert (
        result["regime"] == "rising" and result["budget"] == 0 and result["over_budget"]
    )


def test_budget_tracker_without_vix_is_unknown(tmp_path):
    broker = FakeAdapter(account_id="DU1", read_only=True)
    asyncio.run(broker.connect())
    tracker = BudgetTracker(
        TradingConfig(), Journal(tmp_path / "j.db"), clock=lambda: NOW
    )
    result = asyncio.run(tracker.update(broker, [], ACCOUNT))
    assert result["regime"] == "unknown" and result["multiplier"] == 0


def test_regime_cached_for_five_minutes_and_journal_throttled(tmp_path):
    clock = [NOW]
    broker = broker_with([22, 23, 24, 25, 24, 23])
    tracker = BudgetTracker(
        TradingConfig(), Journal(tmp_path / "j.db"), clock=lambda: clock[0]
    )
    for _ in range(3):
        asyncio.run(tracker.update(broker, [], ACCOUNT))
        clock[0] += timedelta(minutes=1)
    assert broker.history_calls == 1
    assert Journal(tmp_path / "j.db").count("budget_snapshot") == 1
    clock[0] += timedelta(minutes=15)
    asyncio.run(tracker.update(broker, [], ACCOUNT))
    assert broker.history_calls == 2
    assert Journal(tmp_path / "j.db").count("budget_snapshot") == 2


def test_engine_reports_budget_and_page_shows_it(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = broker_with([22, 23, 24, 25, 24, 23])
    asyncio.run(broker.disconnect())
    broker.set_position(XSP, -10, 0.65)
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
        )
    )
    beat = json.loads(hb.read_text())
    assert beat["budget"]["regime"] == "elevated" and beat["budget"]["used"] > 0

    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(hb))
    monkeypatch.setenv("TRADING_HALT_FILE", str(tmp_path / "H"))
    html = create_app().test_client().get("/bot").get_data(as_text=True)
    assert "Risk budget" in html and "Regime: <strong>elevated</strong>" in html
