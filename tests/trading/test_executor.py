"""Executor: intents to orders under mode gating (plan T28)."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    OptionContract,
    OrderLeg,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.kill_switch import HaltManager
from src.trading.order_manager import ExecStatus, OrderManager

MID_SESSION = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)  # Monday 11:00 ET
EARLY = datetime(2026, 11, 23, 14, 35, tzinfo=timezone.utc)  # 09:35 ET
PUT = OptionContract(
    "XSP",
    date(2026, 12, 31),
    600,
    Right.PUT,
    style=ExerciseStyle.EUROPEAN,
    cash_settled=True,
    trading_class="XSP",
)


def make_env(tmp_path, mode="paper", clock=MID_SESSION, read_only=False, el=700_000):
    broker = FakeAdapter(account_id="DU1", read_only=read_only, clock=lambda: clock)
    asyncio.run(broker.connect())
    broker.set_account(excess_liquidity=el)
    broker.set_position(PUT, -2, 2.0)
    broker.set_quote(
        Quote(PUT, 1.00, 1.02, clock, underlying_price=650, greeks=Greeks(-0.1, iv=0.2))
    )
    journal = Journal(tmp_path / "j.db")
    config = TradingConfig.model_validate({"mode": mode})
    halts = HaltManager(journal, tmp_path / "HALT")

    async def no_sleep(_):
        return None

    orders = OrderManager(broker, journal, sleep=no_sleep)
    ex = Executor(
        broker,
        journal,
        config,
        halts,
        orders,
        ApprovalQueue(journal, clock=lambda: clock),
        clock=lambda: clock,
    )
    return ex, broker, journal


def close_intent(strategy="position_manager", iid="close-1"):
    return TradeIntent(
        iid,
        strategy,
        IntentKind.CLOSE,
        (OrderLeg(PUT, Action.BUY, 2),),
        "T",
        MID_SESSION,
        True,
    )


def open_intent(qty=1):
    later = OptionContract(
        "XSP",
        date(2027, 1, 29),
        580,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
        trading_class="XSP",
    )
    return (
        TradeIntent(
            "open-1",
            "index_put",
            IntentKind.OPEN,
            (OrderLeg(later, Action.SELL, qty),),
            "SCAN",
            MID_SESSION,
        ),
        later,
    )


def run_(c):
    return asyncio.run(c)


def test_paper_mode_sends_a_close(tmp_path):
    ex, broker, journal = make_env(tmp_path)
    res = run_(ex.submit(close_intent(), emergency=True))
    assert res.status is ExecStatus.FILLED
    assert run_(broker.positions()) == []


def test_outside_window_waits_but_restoration_may_go(tmp_path):
    ex, broker, _ = make_env(tmp_path, clock=EARLY)
    assert run_(ex.submit(close_intent())) is None  # 09:35 ET: before 09:45
    res = run_(ex.submit(close_intent("margin_restoration", "r-1"), emergency=True))
    assert res.status is ExecStatus.FILLED  # restoration allowed from 09:31


def test_weekend_nothing_goes(tmp_path):
    ex, _, _ = make_env(
        tmp_path, clock=datetime(2026, 11, 28, 16, 0, tzinfo=timezone.utc)
    )
    assert run_(ex.submit(close_intent("margin_restoration", "r-1"))) is None


def test_shadow_logs_only(tmp_path):
    ex, broker, journal = make_env(tmp_path, mode="shadow")
    assert run_(ex.submit(close_intent())) is None
    [ev] = [r.data for r in journal.read("order_events")]
    assert ev["event"] == "shadow" and "buy 2 XSP" in ev["summary"]
    assert run_(broker.positions())[0].quantity == -2


def test_approve_mode_asks_then_sends(tmp_path):
    ex, broker, journal = make_env(tmp_path, mode="approve")
    assert run_(ex.submit(close_intent())) is None
    assert ex.approvals.status("close-1") is ApprovalStatus.PENDING
    ex.approvals.approve("close-1", by="owner")
    res = run_(ex.submit(close_intent(), emergency=True))
    assert res.status is ExecStatus.FILLED


def test_gate_blocks_opening_that_breaks_margin_floor(tmp_path):
    ex, broker, journal = make_env(tmp_path, el=130_000)
    intent, later = open_intent()
    broker.set_quote(
        Quote(
            later,
            3.0,
            3.1,
            MID_SESSION,
            underlying_price=650,
            greeks=Greeks(-0.12, iv=0.2),
        )
    )
    res = run_(ex.submit(intent))
    assert res.status is ExecStatus.REJECTED and "el_after" in res.detail


def test_gate_runs_limits_and_stress_for_openings(tmp_path):
    ex, broker, journal = make_env(tmp_path)
    intent, later = open_intent()
    broker.set_quote(
        Quote(
            later,
            3.0,
            3.1,
            MID_SESSION,
            underlying_price=650,
            greeks=Greeks(-0.12, iv=0.2),
        )
    )
    res = run_(ex.submit(intent))
    checks = {c["name"] for c in journal.latest("risk_checks").data["checks"]}
    assert {
        "halt",
        "el_after",
        "short_put_notional",
        "stress_budget",
        "stress_el",
    } <= checks


def test_opening_halt_blocks_opening_not_close(tmp_path):
    ex, broker, journal = make_env(tmp_path)
    ex.halts.halt("test")
    intent, later = open_intent()
    broker.set_quote(
        Quote(later, 3.0, 3.1, MID_SESSION, underlying_price=650, greeks=Greeks(-0.12))
    )
    assert run_(ex.submit(intent)).status is ExecStatus.REJECTED
    assert run_(ex.submit(close_intent(), emergency=True)).status is ExecStatus.FILLED


def test_engine_restores_margin_on_paper(tmp_path, monkeypatch):
    """Breach -> the engine closes the naked put on its own (paper mode)."""
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_account(excess_liquidity=95_000, net_liquidation=200_000)
    broker.set_position(PUT, -2, 2.0)
    broker.set_quote(
        Quote(
            PUT,
            1.00,
            1.02,
            datetime.now(timezone.utc),
            underlying_price=650,
            greeks=Greeks(-0.1, iv=0.2),
        )
    )

    async def no_sleep(_):
        return None

    real_om = run.OrderManager
    monkeypatch.setattr(
        run, "OrderManager", lambda *a, **kw: real_om(*a, sleep=no_sleep, **kw)
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
    asyncio.run(broker.connect())  # the engine disconnects on exit
    assert asyncio.run(broker.positions()) == []  # closed
    events = [
        r.data.get("event") for r in Journal(tmp_path / "j.db").read("order_events")
    ]
    assert "placed" in events
