"""Assignment & Event Guard triggers (plan T28a, spec §5.11)."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.calendars import Dividend
from src.trading.config import TradingConfig
from src.trading.domain import (
    ExerciseStyle,
    Greeks,
    OptionContract,
    Position,
    Quote,
    Right,
)
from src.trading.guard import check_position, scan
from src.trading.journal import Journal

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig.model_validate({"watchlist": {"QQQM": {"call_roll_delta": 0.30}}})


def opt(sym, strike, right, dte=30, cash=False):
    return OptionContract(
        sym,
        TODAY + timedelta(days=dte),
        strike,
        right,
        style=ExerciseStyle.EUROPEAN if cash else ExerciseStyle.AMERICAN,
        cash_settled=cash,
    )


def triggers(contract, bid, ask, und, delta, qty=-1, events=None, cfg=CFG):
    pos = Position(contract, qty, 1.0)
    q = Quote(contract, bid, ask, NOW, underlying_price=und, greeks=Greeks(delta))
    return {s.trigger for s in check_position(pos, q, TODAY, cfg, events)}


def test_quiet_position_has_no_signals():
    assert (
        triggers(opt("XSP", 600, Right.PUT, cash=True), 0.9, 1.0, 660, -0.12) == set()
    )


def test_early_roll_by_delta_with_per_symbol_call_threshold():
    assert "early_roll" in triggers(
        opt("XSP", 600, Right.PUT, cash=True), 3, 3.2, 620, -0.36
    )
    assert "early_roll" not in triggers(
        opt("XSP", 600, Right.PUT, cash=True), 3, 3.2, 620, -0.34
    )
    assert "early_roll" in triggers(
        opt("QQQM", 260, Right.CALL), 1, 1.1, 250, 0.31
    )  # QQQM 0.30
    assert "early_roll" not in triggers(
        opt("SPY", 700, Right.CALL), 1, 1.1, 670, 0.31
    )  # default 0.40


def test_low_extrinsic_only_for_american():
    # QQQM call 240 with stock at 250: intrinsic 10, mid 10.05 -> 0.05 time value.
    assert "low_extrinsic" in triggers(
        opt("QQQM", 240, Right.CALL), 10.0, 10.1, 250, 0.95
    )
    # Same on a cash-settled index put: no early assignment possible.
    assert "low_extrinsic" not in triggers(
        opt("XSP", 700, Right.PUT, cash=True), 40.0, 40.1, 660, -0.95
    )


def test_pin_risk_and_expiration_day():
    assert "pin_risk" in triggers(
        opt("XSP", 600, Right.PUT, dte=3, cash=True), 1, 1.1, 605, -0.4
    )
    s = triggers(opt("XSP", 600, Right.PUT, dte=0, cash=True), 2, 2.1, 598, -0.6)
    assert "expiration_day" in s
    assert "expiration_day" not in triggers(
        opt("XSP", 600, Right.PUT, dte=0, cash=True), 0, 0.05, 640, -0.01
    )


class Events:
    def __init__(self, div_risk=False, div=None, earnings=False):
        self.div_risk, self.div, self.earnings = div_risk, div, earnings

    def ex_dividend_risk(self, *a):
        return self.div_risk

    def ex_dividend_before(self, *a):
        return self.div

    def earnings_before(self, *a):
        return self.earnings


def test_ex_dividend_triggers():
    call = opt("QQQM", 240, Right.CALL)
    div = Dividend(TODAY + timedelta(days=5), 0.33)
    assert "ex_dividend" in triggers(
        call, 10.1, 10.3, 250, 0.9, events=Events(True, div)
    )
    assert "ex_dividend" not in triggers(
        call, 11.0, 11.2, 250, 0.9, events=Events(True, div)
    )  # rich
    assert "ex_dividend_unknown" in triggers(
        call, 10.1, 10.3, 250, 0.9, events=Events(None)
    )


def test_earnings_trigger_needs_delta():
    put = opt("AAPL", 200, Right.PUT)
    assert "earnings" in triggers(put, 3, 3.2, 205, -0.30, events=Events(earnings=True))
    assert "earnings" not in triggers(
        put, 1, 1.2, 230, -0.15, events=Events(earnings=True)
    )


def test_long_options_ignored_and_scan_sorted():
    hedge = opt("SPY", 560, Right.PUT)
    assert triggers(hedge, 30, 31, 540, -0.9, qty=5) == set()
    a, b = opt("XSP", 600, Right.PUT, dte=0, cash=True), opt(
        "XSP", 590, Right.PUT, cash=True
    )
    quotes = {
        a.key: Quote(a, 2, 2.1, NOW, underlying_price=598, greeks=Greeks(-0.6)),
        b.key: Quote(b, 3, 3.2, NOW, underlying_price=598, greeks=Greeks(-0.4)),
    }
    sigs = scan([Position(b, -1, 1.0), Position(a, -1, 1.0)], quotes, TODAY, CFG)
    assert sigs[0].severity == "critical" and sigs[0].trigger == "expiration_day"


def test_engine_reports_guard_signals(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = FakeAdapter(account_id="DU1", read_only=True)
    c = opt("XSP", 600, Right.PUT, cash=True)
    broker.set_position(c, -2, 1.0)
    broker.set_quote(Quote(c, 6, 6.2, NOW, underlying_price=610, greeks=Greeks(-0.42)))

    class Rec:
        name = "rec"

        def __init__(self):
            self.alerts = []

        def send(self, a):
            self.alerts.append(a)

    from src.trading.config import AlertsConfig
    from src.trading.notifier import Notifier

    rec = Rec()
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
            notifier=Notifier(AlertsConfig(min_level="info"), [rec]),
        )
    )
    beat = json.loads(hb.read_text())
    assert [g["trigger"] for g in beat["guard"]] == ["early_roll"]
    assert any(a.title.startswith("Roll needed") for a in rec.alerts)


def test_engine_plans_a_roll_for_a_triggered_position(tmp_path, monkeypatch):
    from src.trading.broker.base import OptionChain

    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = FakeAdapter(account_id="DU1", read_only=True)
    old = OptionContract(
        "XSP",
        TODAY + timedelta(days=10),
        600,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
        trading_class="XSP",
    )
    later = TODAY + timedelta(days=40)
    broker.set_chain(
        "XSP", [OptionChain("XSP", "XSP", 100, (old.expiry, later), (590.0, 600.0))]
    )
    broker.set_position(old, -1, 3.0)
    broker.set_quote(
        Quote(old, 3.9, 4.1, NOW, underlying_price=605, greeks=Greeks(-0.42, iv=0.2))
    )
    for k, mid in ((590.0, 4.6), (600.0, 7.0)):
        c = OptionContract(
            "XSP",
            later,
            k,
            Right.PUT,
            style=ExerciseStyle.EUROPEAN,
            cash_settled=True,
            trading_class="XSP",
        )
        broker.set_quote(
            Quote(
                c,
                mid - 0.1,
                mid + 0.1,
                NOW,
                underlying_price=605,
                greeks=Greeks(-0.4, iv=0.2),
            )
        )
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
        )
    )
    [g] = json.loads(hb.read_text())["guard"]
    assert g["plan"].startswith("roll: roll to XSP") and "590" in g["plan"]
