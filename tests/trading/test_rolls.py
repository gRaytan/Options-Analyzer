"""Roll builder and roll chains (plan T28b, spec §5.11)."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    OptionContract,
    Position,
    Quote,
    Right,
)
from src.trading.journal import Journal
from src.trading.order_manager import ExecStatus, OrderManager
from src.trading.risk.checks import CheckResult, GateDecision
from src.trading.rolls import (
    ChainBook,
    candidate_contracts,
    chain_loss_multiple,
    choose_roll,
    plan_roll,
    roll_for,
)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig()
ACCOUNT = AccountSnapshot("DU1", NOW, 1_000_000, 700_000, 300_000, 300_000, 650_000)
D10, D35, D45, D63, D120 = (TODAY + timedelta(days=d) for d in (10, 35, 45, 63, 120))
CHAIN = OptionChain(
    "XSP", "XSP", 100, (D10, D35, D45, D63, D120), (580.0, 590.0, 600.0, 610.0)
)


def xsp(expiry, strike, right=Right.PUT):
    return OptionContract(
        "XSP",
        expiry,
        strike,
        right,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
        trading_class="XSP",
    )


OLD = xsp(D10, 600)


def q(c, mid, delta=-0.3, iv=0.2, und=605):
    return Quote(
        c,
        round(mid - 0.05, 2),
        round(mid + 0.05, 2),
        NOW,
        underlying_price=und,
        greeks=Greeks(delta, iv=iv),
    )


def test_candidates_window_and_direction():
    puts = candidate_contracts(OLD, [CHAIN], TODAY, CFG)
    assert {c.expiry for c in puts} == {
        D35,
        D45,
    }  # 30-60 DTE preferred; not 10, 63 or 120
    assert {c.strike for c in puts} == {580.0, 590.0, 600.0}  # same or lower for puts
    call = xsp(D10, 600, Right.CALL)
    assert {c.strike for c in candidate_contracts(call, [CHAIN], TODAY, CFG)} == {
        600.0,
        610.0,
    }


def test_candidates_skip_expiries_spanning_earnings():
    aapl = OptionContract("AAPL", D10, 200, Right.PUT)
    chain = OptionChain("AAPL", "AAPL", 100, (D35, D45), (190.0, 200.0))

    class Events:
        def earnings_before(self, sym, expiry, today):
            return expiry >= D45

    got = candidate_contracts(aapl, [chain], TODAY, CFG, Events())
    assert {c.expiry for c in got} == {D35}


def test_choose_prefers_strike_improvement_with_credit():
    old_q = q(OLD, 4.00)
    cands = {
        xsp(D35, 600).key: q(xsp(D35, 600), 6.00),  # same strike, +2.00
        xsp(D45, 590).key: q(xsp(D45, 590), 4.30),  # 10 lower, +0.30
        xsp(D45, 580).key: q(xsp(D45, 580), 4.02),  # 20 lower, only +0.02 -> too little
    }
    best = choose_roll(OLD, old_q, cands, ACCOUNT, CFG, TODAY)
    assert best.new == xsp(D45, 590) and best.net_credit == pytest.approx(0.30)
    assert best.stress_after <= best.stress_before


def test_choose_rejects_debit_and_more_crash_risk():
    old_q = q(OLD, 4.00)
    debit = {xsp(D35, 580).key: q(xsp(D35, 580), 3.50)}
    assert choose_roll(OLD, old_q, debit, ACCOUNT, CFG, TODAY) is None
    # A far OTM 10-day put rolled out to 45 days at the same strike carries more
    # crash loss (more time value left when the market drops): rejected.
    far = xsp(D10, 550)
    out = xsp(D45, 550)
    risky = {out.key: q(out, 1.50, delta=-0.08, und=640)}
    assert (
        choose_roll(far, q(far, 0.20, delta=-0.02, und=640), risky, ACCOUNT, CFG, TODAY)
        is None
    )


def test_chain_book_and_loss(tmp_path):
    book = ChainBook(Journal(tmp_path / "j.db"))
    pos = Position(OLD, -2, 1.00)
    chain = book.ensure(pos)
    assert book.ensure(pos)["chain_id"] == chain["chain_id"]
    rolled = book.record_roll(chain, xsp(D45, 590).key, 0.30)
    assert rolled["rolls"] == 1 and rolled["cumulative_credit"] == pytest.approx(1.30)
    assert book.for_position(OLD.key) is None and book.for_position(xsp(D45, 590).key)
    assert chain_loss_multiple(rolled, 5.20) == pytest.approx(
        3.9
    )  # (5.20 - 1.30) / 1.00
    book.record_close(rolled, 5.20)
    assert book.for_position(xsp(D45, 590).key) is None


def plan(chain_overrides=None, mid=4.0, cands=None):
    chain = {
        "chain_id": "c1",
        "rolls": 0,
        "original_credit": 2.0,
        "cumulative_credit": 2.0,
        "current_key": OLD.key,
        "closed": False,
    }
    chain.update(chain_overrides or {})
    cands = (
        cands if cands is not None else {xsp(D45, 590).key: q(xsp(D45, 590), mid + 0.3)}
    )
    return plan_roll(
        Position(OLD, -2, 2.0),
        q(OLD, mid),
        cands,
        ACCOUNT,
        CFG,
        chain,
        TODAY,
        "early_roll",
        NOW,
    )


def test_plan_roll_intent():
    p = plan()
    assert p.action == "roll"
    it = p.intent
    assert it.kind is IntentKind.ROLL and not it.risk_reducing and it.chain_id == "c1"
    assert [(l.action, l.quantity) for l in it.legs] == [
        (Action.BUY, 2),
        (Action.SELL, 2),
    ]
    assert it.details["worst_price"] == -0.05


@pytest.mark.parametrize(
    "overrides,mid,cands,reason",
    [
        ({"rolls": 4}, 4.0, None, "rolled 4 times"),
        ({}, 4.0, {}, "no roll"),
        ({}, 8.1, None, "3x"),  # (8.1 - 2.0) / 2.0 = 3.05x
    ],
)
def test_plan_closes_instead(overrides, mid, cands, reason):
    p = plan(overrides, mid, cands)
    assert p.action == "close" and reason in p.reason
    assert p.intent.kind is IntentKind.CLOSE and p.intent.risk_reducing


def test_order_manager_honors_worst_price(tmp_path):
    broker = FakeAdapter(account_id="DU1")
    asyncio.run(broker.connect())
    broker.set_position(OLD, -2, 2.0)
    new = xsp(D45, 590)
    broker.set_quote(q(OLD, 4.00))
    broker.set_quote(q(new, 4.04))  # mid credit only 0.04 < 0.05 minimum
    p = plan(cands={new.key: q(new, 4.30)})  # planned when the credit was 0.30

    async def quotes_fn(contracts):
        return {c.key: await broker.quote(c) for c in contracts}

    async def gate(order, intent, quotes):
        return GateDecision((CheckResult("ok", True),))

    async def no_sleep(_):
        return None

    om = OrderManager(broker, Journal(tmp_path / "j.db"), sleep=no_sleep)
    res = asyncio.run(om.execute(p.intent, quotes_fn, gate, emergency=True))
    assert res.status is not ExecStatus.FILLED  # never filled below the minimum credit
    assert all(price <= -0.05 + 1e-9 for price in res.prices)


def test_roll_for_with_broker(tmp_path):
    broker = FakeAdapter(account_id="DU1")
    asyncio.run(broker.connect())
    broker.set_chain("XSP", [CHAIN])
    broker.set_quote(q(OLD, 4.00))
    for c in candidate_contracts(OLD, [CHAIN], TODAY, CFG):
        broker.set_quote(q(c, 4.00 + (0.5 if c.strike == 590 else 0.01)))
    book = ChainBook(Journal(tmp_path / "j.db"))
    p = asyncio.run(
        roll_for(
            broker,
            Position(OLD, -2, 2.0),
            ACCOUNT,
            CFG,
            book,
            TODAY,
            "early_roll",
            now=NOW,
        )
    )
    assert p.action == "roll" and p.candidate.new.strike == 590.0 and p.considered == 6
