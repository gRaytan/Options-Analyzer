"""Automatic margin restoration (plan T26, spec §6.5)."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    ExerciseStyle,
    Greeks,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
)
from src.trading.journal import Journal
from src.trading.margin_restoration import candidates, el_needed, plan, restore
from src.trading.order_manager import ExecStatus, ExecutionResult, OrderManager
from src.trading.risk.checks import CheckResult, GateDecision

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig()
EXP = TODAY + timedelta(days=30)


def opt(sym, strike, right, cash=False):
    return OptionContract(
        sym,
        EXP,
        strike,
        right,
        style=ExerciseStyle.EUROPEAN if cash else ExerciseStyle.AMERICAN,
        cash_settled=cash,
    )


XSP_A, XSP_B = opt("XSP", 600, Right.PUT, True), opt("XSP", 620, Right.PUT, True)
QQQM_CALL, HEDGE = opt("QQQM", 270, Right.CALL), opt("SPY", 560, Right.PUT)
QQQM = StockContract("QQQM")


def q(c, bid, ask, und):
    return Quote(c, bid, ask, NOW, underlying_price=und, greeks=Greeks(-0.2, iv=0.2))


QUOTES = {
    XSP_A.key: q(XSP_A, 2.0, 2.1, 640),
    XSP_B.key: q(XSP_B, 5.0, 5.4, 640),
    QQQM_CALL.key: q(QQQM_CALL, 0.5, 0.6, 250),
    HEDGE.key: q(HEDGE, 3.0, 3.2, 640),
}
POSITIONS = [
    Position(QQQM, 300, 180.0, 250.0),
    Position(QQQM_CALL, -3, 1.0),  # fully covered by 300 shares
    Position(HEDGE, 5, 3.0),  # long hedge
    Position(XSP_A, -4, 3.0),
    Position(XSP_B, -2, 4.0),
]
ACCOUNT = AccountSnapshot("DU1", NOW, 300_000, 95_000, 205_000, 205_000, 90_000)


def test_el_needed_takes_the_larger_of_both_targets():
    assert el_needed(ACCOUNT, 95_000, CFG) == pytest.approx(
        15_000
    )  # $110k vs 35% of $300k = $105k
    big = AccountSnapshot("DU1", NOW, 1_000_000, 280_000, 0, 0, 0)
    assert el_needed(big, 280_000, CFG) == pytest.approx(70_000)  # cushion 35% binds
    assert el_needed(big, 400_000, CFG) == 0


def test_only_naked_short_options_are_candidates():
    keys = {c.position.contract.key for c in candidates(POSITIONS, QUOTES, CFG, set())}
    assert keys == {XSP_A.key, XSP_B.key}  # no stock, hedge, or covered call


def test_partially_covered_call_counts_its_naked_part():
    positions = [Position(QQQM, 100, 180.0, 250.0), Position(QQQM_CALL, -3, 1.0)]
    [c] = candidates(positions, QUOTES, CFG, set())
    assert c.margin_freed == pytest.approx(
        2 * 0.20 * 250 * 100 - 2 * 20 * 100 + 2 * 0.55 * 100, rel=0.01
    )


def test_plan_prefers_flagged_then_cheapest_and_stops_at_need():
    p = plan(POSITIONS, QUOTES, ACCOUNT, 95_000, CFG)
    assert p.sufficient and len(p.closes) == 1  # one XSP position frees > $15k
    first = p.closes[0]
    assert first.position.contract.key == XSP_A.key  # tighter spread: cheaper margin
    flagged = plan(POSITIONS, QUOTES, ACCOUNT, 95_000, CFG, flagged={XSP_B.key})
    assert flagged.closes[0].position.contract.key == XSP_B.key


def test_plan_uses_ib_what_if_when_given():
    p = plan(
        POSITIONS,
        QUOTES,
        ACCOUNT,
        95_000,
        CFG,
        margin_freed={XSP_A.key: 1_000, XSP_B.key: 30_000},
    )
    assert p.closes[0].position.contract.key == XSP_B.key


def test_nothing_to_do_when_restored():
    assert plan(POSITIONS, QUOTES, ACCOUNT, 200_000, CFG).closes == []


def test_restore_stops_once_covered_and_skips_failures():
    p = plan(POSITIONS, QUOTES, ACCOUNT, 60_000, CFG)  # needs $50k: both positions
    assert len(p.closes) == 2
    calls = []

    async def execute(intent, emergency):
        calls.append((intent.legs[0].contract.key, emergency, intent.risk_reducing))
        status = ExecStatus.UNFILLED if len(calls) == 1 else ExecStatus.FILLED
        return ExecutionResult(
            intent.intent_id, status, filled=0 if len(calls) == 1 else 2
        )

    result = asyncio.run(restore(p, execute, emergency=True, today=TODAY, now=NOW))
    assert [c[1] for c in calls] == [True, True] and all(c[2] for c in calls)
    assert result.gained == pytest.approx(
        p.closes[1].el_gain
    )  # only the filled one counts


def test_end_to_end_with_order_manager(tmp_path):
    broker = FakeAdapter(account_id="DU1")
    asyncio.run(broker.connect())
    for pos in POSITIONS:
        broker.set_position(pos.contract, pos.quantity, pos.avg_cost)
    for quote in QUOTES.values():
        broker.set_quote(quote)

    async def no_sleep(_):
        return None

    om = OrderManager(broker, Journal(tmp_path / "j.db"), sleep=no_sleep)

    async def quotes_fn(contracts):
        return {c.key: await broker.quote(c) for c in contracts}

    async def gate(order, intent, quotes):
        return GateDecision((CheckResult("ok", True),))

    async def execute(intent, emergency):
        return await om.execute(intent, quotes_fn, gate, emergency=emergency)

    p = plan(POSITIONS, QUOTES, ACCOUNT, 95_000, CFG)
    result = asyncio.run(restore(p, execute, emergency=True, today=TODAY, now=NOW))
    assert result.restored
    held = {pos.contract.key: pos.quantity for pos in asyncio.run(broker.positions())}
    assert XSP_A.key not in held  # closed
    assert held[QQQM.key] == 300 and held[HEDGE.key] == 5 and held[QQQM_CALL.key] == -3
