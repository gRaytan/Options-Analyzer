"""Crash hedge: the collar (XSP puts paid by covered calls), approved per order."""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from py_vollib.black_scholes import black_scholes

from src.trading import hedge, run
from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    Bar,
    ExerciseStyle,
    Greeks,
    HistoryKind,
    IntentKind,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
    utcnow,
)
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.mode_policy import Route, route
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import Scenario, StressModel, betas_from

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig.model_validate(
    {
        "enabled_strategies": {"tail_hedge": {"manage": True, "open": True}},
        "watchlist": {"QQQM": {"beta": 1.2}, "NBIS": {"beta": 2.5}},
    }
)
SPY, QQQM, NBIS = StockContract("SPY"), StockContract("QQQM"), StockContract("NBIS")
EXPIRIES = tuple(TODAY + timedelta(days=d) for d in (30, 60, 120, 175, 210, 300))
XSP_CHAIN = OptionChain(
    "XSP", "XSP", 100, EXPIRIES, tuple(float(k) for k in range(500, 760, 5))
)


def acct(nlv=1_000_000):
    return AccountSnapshot("DU1", NOW, nlv, 700_000, 300_000, 350_000, 650_000, 0.0)


def book():
    return [
        Position(SPY, 500, 600.0, 670.0),  # $335k x 1.0
        Position(QQQM, 1200, 220.0, 250.0),  # $300k x 1.2 = $360k
        Position(NBIS, 300, 80.0, 100.0),  # $30k x 2.5 = $75k
    ]


def xsp_put(k, days):
    return OptionContract(
        "XSP",
        TODAY + timedelta(days=days),
        k,
        Right.PUT,
        100,
        ExerciseStyle.EUROPEAN,
        True,
        "XSP",
    )


def price(c, und, iv=0.25):
    t = (c.expiry - TODAY).days / 365
    flag = "p" if c.right is Right.PUT else "c"
    return float(black_scholes(flag, und, c.strike, t, 0.04, iv))


def quote(c, und, iv=0.25):
    mid = price(c, und, iv)
    return Quote(
        c,
        round(mid * 0.98, 2),
        round(mid * 1.02, 2),
        utcnow(),
        underlying_price=und,
        greeks=Greeks(-0.1, iv=iv),
    )


def test_beta_exposure_and_need():
    assert hedge.beta_exposure(book(), CFG) == pytest.approx(335_000 + 360_000 + 75_000)
    n = hedge.need(book(), CFG, 723.7, TODAY)
    assert n.target == pytest.approx(770_000) and n.covered == 0
    assert n.contracts == math.ceil(770_000 / (723.7 * 0.9 * 100))
    held = book() + [
        Position(xsp_put(650, 150), 6, 9.0),
        Position(xsp_put(650, 40), 6, 2.0),
    ]
    n2 = hedge.need(held, CFG, 723.7, TODAY)
    assert n2.covered == 650 * 100 * 6  # the 40-day puts no longer count
    assert [p.contract.key for p in n2.roll] == [xsp_put(650, 40).key]


def test_pick_put_and_call():
    put = hedge.pick_put(XSP_CHAIN, 723.7, TODAY, CFG)
    assert put.strike == 650.0 and put.cash_settled  # <= 90% of 723.7 = 651.3
    assert (put.expiry - TODAY).days == 175  # closest to the middle of 120-240
    spy_chain = OptionChain(
        "SPY", "SPY", 100, EXPIRIES, tuple(float(k) for k in range(600, 760, 5))
    )
    call = hedge.pick_call(spy_chain, "SPY", 670.0, TODAY, CFG)
    assert call.strike == 725.0 and call.right is Right.CALL  # >= 108% of 670 = 723.6


def plan_for(positions, cfg=CFG, nlv=1_000_000):
    put = hedge.pick_put(XSP_CHAIN, 723.7, TODAY, cfg)
    spy_chain = OptionChain(
        "SPY", "SPY", 100, EXPIRIES, tuple(float(k) for k in range(600, 760, 5))
    )
    call = hedge.pick_call(spy_chain, "SPY", 670.0, TODAY, cfg)
    quotes = {put.key: quote(put, 723.7)}
    return hedge.plan(
        positions,
        acct(nlv),
        cfg,
        723.7,
        XSP_CHAIN,
        quotes,
        {"SPY": (call, quote(call, 670.0, 0.2))},
        TODAY,
        NOW,
    )


def test_collar_plan_needs_approval_for_each_order():
    p = plan_for(book())
    put_i, call_i = p.intents
    assert put_i.kind is IntentKind.HEDGE and put_i.legs[0].action is Action.BUY
    assert put_i.legs[0].quantity == min(p.need.contracts, CFG.risk.max_contracts_xsp)
    assert (
        call_i.legs[0].action is Action.SELL and call_i.legs[0].quantity == 2
    )  # half of 500 shares
    assert p.call_credit > 0 and p.put_cost > p.call_credit
    for it in p.intents:
        assert it.details["require_approval"] and route(it, CFG, False) is Route.ASK
    assert put_i.details["worst_price"] > 0 and call_i.details["worst_price"] < 0


def test_puts_only_style_and_debit_cap():
    puts_only = CFG.model_copy(
        update={"tail_hedge": CFG.tail_hedge.model_copy(update={"style": "puts"})}
    )
    assert [i.legs[0].contract.right for i in plan_for(book(), puts_only).intents] == [
        Right.PUT
    ]
    small = plan_for(book(), nlv=100_000)  # 2% of $100k = $2k: too little for the puts
    assert small.intents == [] and "above the 2% of NLV limit" in small.notes[-1]


def test_nothing_to_do_when_covered():
    covered = book() + [Position(xsp_put(650, 175), 12, 9.0)]  # $780k >= $770k target
    p = plan_for(covered)
    assert p.intents == [] and "nothing to buy" in p.notes[0]


def test_collar_keeps_ex_out_of_a_margin_call_at_minus_20():
    """The point of it: Excess Liquidity in a 20% crash, with and without the collar."""
    positions = book()
    p = plan_for(positions)
    put_leg = p.intents[0].legs[0]
    hedged = positions + [Position(put_leg.contract, put_leg.quantity, 9.0, 9.0)]
    quotes = {put_leg.contract.key: quote(put_leg.contract, 723.7)}

    def el(pos):
        m = StressModel(
            exposures_from(pos, quotes), quotes, acct(), TODAY, betas_from(CFG)
        )
        return m.run(Scenario("-20%", -0.20, 0.50, 1.5))

    bare, with_puts = el(positions), el(hedged)
    gain = with_puts.options_pnl - bare.options_pnl
    assert gain > 50_000  # the puts pay in cash when the market falls 20%
    assert with_puts.excess_liquidity_after > bare.excess_liquidity_after + 50_000


def test_engine_plans_once_and_buys_after_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.fill_threshold = 0.3  # fills between mid and the ask, as in real markets
    for p in book():
        broker.set_position(p.contract, p.quantity, p.avg_cost)
    broker.set_history(
        "XSP",
        HistoryKind.PRICE,
        [Bar(TODAY - timedelta(days=1), 723.7, 724, 720, 723.7)],
    )
    broker.set_chain("XSP", [XSP_CHAIN])
    broker.set_chain(
        "SPY",
        [
            OptionChain(
                "SPY", "SPY", 100, EXPIRIES, tuple(float(k) for k in range(600, 760, 5))
            )
        ],
    )
    broker.set_chain(
        "QQQM",
        [
            OptionChain(
                "QQQM", "QQQM", 100, EXPIRIES, tuple(float(k) for k in range(220, 300))
            )
        ],
    )

    def source(c):
        if isinstance(c, OptionContract):
            und = {"XSP": 723.7, "SPY": 670.0, "QQQM": 250.0}[c.underlying]
            return quote(c, und, 0.25 if c.right is Right.PUT else 0.2)
        return None

    broker.set_quote_source(source)
    for c, px in ((SPY, 670.0), (QQQM, 250.0), (NBIS, 100.0)):
        broker.set_quote(Quote(c, px - 0.01, px + 0.01, utcnow(), last=px))

    async def no_sleep(_):
        return None

    def once():
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
                config=CFG,
                order_sleep=no_sleep,
            )
        )

    once()
    j = Journal(tmp_path / "j.db")
    plan = j.latest("job_runs", key="hedge").data
    puts = [i for i in plan["intents"] if i.legs[0].contract.right is Right.PUT]
    calls = [i for i in plan["intents"] if i.legs[0].contract.right is Right.CALL]
    assert len(puts) == 1 and {c.legs[0].contract.underlying for c in calls} == {
        "SPY",
        "QQQM",
    }
    q = ApprovalQueue(j)
    assert all(q.status(i.intent_id) is ApprovalStatus.PENDING for i in plan["intents"])
    asyncio.run(broker.connect())
    assert not [
        p
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, OptionContract)
    ]
    q.approve(puts[0].intent_id, by="test")
    once()
    asyncio.run(broker.connect())
    held = [
        p
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, OptionContract)
    ]
    assert len(held) == 1 and held[0].quantity == puts[0].legs[0].quantity > 0
    assert (
        len(Journal(tmp_path / "j.db").read("job_runs", key="hedge")) == 1
    )  # planned once today


def test_status_page_shows_the_hedge(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    p = plan_for(book())
    Journal(tmp_path / "j.db").append(
        "job_runs", {**p.to_dict(), "date": TODAY.isoformat()}, key="hedge"
    )
    html = create_app().test_client().get("/bot").get_data(as_text=True)
    assert (
        "Crash hedge (collar)" in html
        and "Puts cover <b>0%</b>" in html
        and "waiting for your approval" in html
    )
