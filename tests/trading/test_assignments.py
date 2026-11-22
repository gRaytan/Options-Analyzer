"""Assignment handling (plan T28c, spec §5.11)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Fill,
    Greeks,
    IntentKind,
    MarginImpact,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
)
from src.trading.journal import Journal
from src.trading.reconcile import expected_from_fills, expected_from_journal, reconcile
from src.trading.assignments import handle_assignment
from src.trading.risk.limits import LimitContext, exposures_from, gate_decision
from src.trading.rolls import ChainBook

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
EXP = TODAY + timedelta(days=20)
PUT = OptionContract("AAPL", EXP, 200, Right.PUT)
CALL = OptionContract("QQQM", EXP, 240, Right.CALL)
XSP = OptionContract(
    "XSP", EXP, 600, Right.PUT, style=ExerciseStyle.EUROPEAN, cash_settled=True
)


def sold(c, n):
    return Fill(f"e-{c.key}", "r", c, Action.SELL, n, 1.0, NOW)


def test_early_put_assignment_is_recognized():
    exp = expected_from_fills([sold(PUT, 2)])
    prev = {PUT.key: -2, "AAPL": 0}
    now = [
        Position(PUT, -1, 1.0),
        Position(StockContract("AAPL"), 100, 200.0),
    ]  # 1 of 2 assigned
    r = reconcile(exp, now, TODAY, prev)
    assert r.ok and not r.should_halt
    [a] = r.assignments
    assert (a.contracts, a.shares) == (1, 100)


def test_call_assignment_on_owner_shares():
    exp = expected_from_fills([sold(CALL, 3)])
    prev = {CALL.key: -3, "QQQM": 600}
    now = [Position(StockContract("QQQM"), 300, 180.0)]
    [a] = reconcile(exp, now, TODAY, prev).assignments
    assert (a.contracts, a.shares) == (3, -300)


def test_share_move_that_does_not_match_stays_a_mismatch():
    exp = expected_from_fills([sold(PUT, 2)])
    prev = {PUT.key: -2, "AAPL": 0}
    now = [Position(StockContract("AAPL"), 150, 200.0)]  # 150 shares: not 200
    r = reconcile(exp, now, TODAY, prev)
    assert r.should_halt and not r.assignments


def test_cash_settled_options_are_never_assignments():
    exp = expected_from_fills([sold(XSP, 1)])
    r = reconcile(exp, [], TODAY, {XSP.key: -1})
    assert r.should_halt and not r.assignments  # XSP gone before expiry: unexplained


def test_handle_put_assignment_plans_sale_and_fixes_expectations(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.append("fills", sold(PUT, 2))
    book = ChainBook(j)
    chain = book.ensure(Position(PUT, -2, 1.0))
    exp = expected_from_journal(j)
    prev = {PUT.key: -2, "AAPL": 0}
    now = [Position(StockContract("AAPL"), 200, 200.0)]
    [a] = reconcile(exp, now, TODAY, prev).assignments
    out = handle_assignment(a, j, TradingConfig(), TODAY, NOW)
    it = out.intent
    assert it.kind is IntentKind.CLOSE and it.risk_reducing
    assert (it.legs[0].contract, it.legs[0].action, it.legs[0].quantity) == (
        StockContract("AAPL"),
        Action.SELL,
        200,
    )
    assert "received 200 AAPL" in out.message
    # The journal now expects the shares, not the option: reconciliation is clean.
    r2 = reconcile(expected_from_journal(j), now, TODAY, {**prev, "AAPL": 200})
    assert r2.ok and not r2.assignments and "AAPL" in r2.matched
    assert book.for_position(PUT.key) is None  # chain closed
    # Idempotent: handling again adds no duplicate fills.
    handle_assignment(a, j, TradingConfig(), TODAY, NOW)
    assert j.count("fills") == 3


def test_handle_call_assignment_plans_rebuy(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.append("fills", sold(CALL, 3))
    exp = expected_from_journal(j)
    [a] = reconcile(
        exp,
        [Position(StockContract("QQQM"), 300, 180.0)],
        TODAY,
        {CALL.key: -3, "QQQM": 600},
    ).assignments
    out = handle_assignment(a, j, TradingConfig(), TODAY, NOW)
    it = out.intent
    assert it.kind is IntentKind.OPEN and not it.risk_reducing
    assert (it.legs[0].action, it.legs[0].quantity) == (Action.BUY, 300)
    assert it.details["restore_assignment"] == 300


def test_keep_options_plan_nothing(tmp_path):
    cfg = TradingConfig.model_validate({"guard": {"on_put_assignment": "keep"}})
    j = Journal(tmp_path / "j.db")
    j.append("fills", sold(PUT, 1))
    [a] = reconcile(
        expected_from_journal(j),
        [Position(StockContract("AAPL"), 100, 1.0)],
        TODAY,
        {PUT.key: -1, "AAPL": 0},
    ).assignments
    assert handle_assignment(a, j, cfg, TODAY, NOW).intent is None


def test_rebuy_skips_concentration_caps_but_not_margin(tmp_path):
    j = Journal(tmp_path / "j.db")
    j.append("fills", sold(CALL, 3))
    [a] = reconcile(
        expected_from_journal(j),
        [Position(StockContract("QQQM"), 300, 180.0)],
        TODAY,
        {CALL.key: -3, "QQQM": 600},
    ).assignments
    rebuy = handle_assignment(a, j, TradingConfig(), TODAY, NOW).intent
    stock = StockContract("QQQM")
    quotes = {stock.key: Quote(stock, 249.9, 250.1, NOW)}
    held = [Position(stock, 300, 180.0, 250.0)]  # $75k = 15% of $500k: at the cap
    account = AccountSnapshot("DU1", NOW, 500_000, 250_000, 150_000, 150_000, 200_000)

    def failures(el_after):
        ctx = LimitContext.from_config(
            TradingConfig(),
            account=account,
            impact=MarginImpact(el_after + 150_000, 1, 150_000),
            exposures=exposures_from(held, quotes),
            quotes=quotes,
        )
        return {c.name for c in gate_decision(rebuy, ctx, TradingConfig()).failures}

    assert "per_name" not in failures(
        240_000
    )  # restoring 300 shares isn't new concentration
    assert "el_after" in failures(100_000)  # but the margin floor still applies


def test_engine_handles_assignment_without_full_halt(tmp_path, monkeypatch):
    import asyncio
    import json

    from src.trading import run
    from src.trading.broker.fake import FakeAdapter

    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    j = Journal(tmp_path / "j.db")
    j.append("fills", sold(PUT, 2))
    j.append("positions_snapshot", (Position(PUT, -2, 1.0),), key="DU1")  # before
    broker = FakeAdapter(account_id="DU1", read_only=True)
    broker.set_position(StockContract("AAPL"), 200, 200.0)  # after: assigned

    alerts = []

    class Rec:
        name = "rec"

        def send(self, a):
            alerts.append(a)

    from src.trading.config import AlertsConfig
    from src.trading.notifier import Notifier

    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=j,
            halt_file=tmp_path / "H",
            notifier=Notifier(AlertsConfig(), [Rec()]),
        )
    )
    beat = json.loads(hb.read_text())
    assert beat["assignments"] == 1 and beat["halted"] is False
    assert any(a.title == "Option assigned" for a in alerts)
    j2 = Journal(tmp_path / "j.db")
    [it] = [r.data for r in j2.read("intents")]
    assert it.trigger == "PUT_ASSIGNED"
