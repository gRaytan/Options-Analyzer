"""Entries from ready scanner candidates (plan T33)."""
import asyncio
import json
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import StrategyName, TradingConfig
from src.trading.domain import (
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
)
from src.trading.entries import entry_intents, notional_room, size_put, worst_price
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.scanner import Candidate, ScanResult

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
EXP = TODAY + timedelta(days=38)
CFG = TradingConfig()


def cand(
    strategy, contract, stress=7_000.0, contracts=1, fire=True, score=1.0, mid=3.0
):
    q = Quote(
        contract,
        mid - 0.05,
        mid + 0.05,
        NOW,
        underlying_price=650,
        greeks=Greeks(-0.13),
    )
    return Candidate(
        strategy,
        contract,
        q,
        contracts,
        mid * 100,
        mid * 100 - 2.3,
        100.0,
        stress,
        0.2,
        score,
        38,
        raw_score=score,
        timing=1.2 if fire else 1.0,
        fire=fire,
    )


def xsp(k):
    return OptionContract(
        "XSP",
        EXP,
        k,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
        trading_class="XSP",
    )


QCALL = OptionContract("QQQM", EXP, 270, Right.CALL)
BUDGET = {
    "free": 100_000.0,
    "multiplier": 1.0,
    "regime": "elevated",
    "spreads_only": False,
}


def test_size_put_fits_free_budget_and_cap():
    assert size_put(cand(StrategyName.INDEX_PUT, xsp(600)), 100_000, CFG) == 14
    assert (
        size_put(cand(StrategyName.INDEX_PUT, xsp(600), stress=1_000), 100_000, CFG)
        == 20
    )  # XSP cap
    assert size_put(cand(StrategyName.INDEX_PUT, xsp(600)), 5_000, CFG) == 0


def test_worst_price_keeps_minimum_credit():
    wp = worst_price(cand(StrategyName.INDEX_PUT, xsp(600)), 2, CFG)
    # ($20 x 2 + 2 x fees) / 200 shares: about 0.21-0.22 per share, as a credit.
    assert -0.25 < wp < -0.20


def test_entries_only_ready_one_per_underlying_and_budget_shared(tmp_path):
    j = Journal(tmp_path / "j.db")
    r = ScanResult(
        [
            cand(StrategyName.INDEX_PUT, xsp(600), score=3.0),
            cand(
                StrategyName.INDEX_PUT, xsp(590), score=2.0
            ),  # same underlying: skipped
            cand(StrategyName.COVERED_CALL, QCALL, stress=0.0, contracts=3, score=1.0),
            cand(StrategyName.INDEX_PUT, xsp(580), fire=False, score=9.0),  # not ready
        ]
    )
    intents = entry_intents(r, BUDGET, CFG, j, TODAY, NOW)
    assert [
        (i.strategy, i.legs[0].contract.strike, i.legs[0].quantity) for i in intents
    ] == [("index_put", 600, 14), ("covered_call", 270, 3)]
    it = intents[0]
    assert (
        it.kind is IntentKind.OPEN
        and not it.risk_reducing
        and it.trigger == "SCAN_READY"
    )
    assert it.legs[0].action is Action.SELL and it.details["worst_price"] < 0


def test_no_put_entries_without_budget_or_in_spreads_only(tmp_path):
    j = Journal(tmp_path / "j.db")
    r = ScanResult(
        [
            cand(StrategyName.INDEX_PUT, xsp(600)),
            cand(StrategyName.COVERED_CALL, QCALL, 0, 3),
        ]
    )
    assert [i.strategy for i in entry_intents(r, None, CFG, j, TODAY, NOW)] == [
        "covered_call"
    ]
    spreads = {**BUDGET, "spreads_only": True}
    assert [i.strategy for i in entry_intents(r, spreads, CFG, j, TODAY, NOW)] == [
        "covered_call"
    ]
    assert [
        i.strategy for i in entry_intents(r, {**BUDGET, "free": 0}, CFG, j, TODAY, NOW)
    ] == ["covered_call"]


def test_one_entry_per_underlying_per_day(tmp_path):
    j = Journal(tmp_path / "j.db")
    r = ScanResult([cand(StrategyName.INDEX_PUT, xsp(600))])
    [first] = entry_intents(r, BUDGET, CFG, j, TODAY, NOW)
    j.append("intents", first, key=first.intent_id)
    assert (
        entry_intents(
            ScanResult([cand(StrategyName.INDEX_PUT, xsp(590))]),
            BUDGET,
            CFG,
            j,
            TODAY,
            NOW,
        )
        == []
    )


def _ivs():
    return [0.10 + 0.20 * (i % 50) / 49 for i in range(251)] + [0.22]


def test_engine_sells_an_index_put_on_paper(tmp_path, monkeypatch):
    """Paper mode, normal margin, elevated VIX, rich XSP premium -> the bot sells a put by itself."""
    from tests.trading.test_scanner import fair_quote, series

    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setenv("TRADING_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    monkeypatch.setattr(
        run,
        "load_trading_config",
        lambda: TradingConfig.model_validate(
            {"enabled_strategies": {"index_put": {"manage": True, "open": True}}}
        ),
    )
    real_om = run.OrderManager

    async def no_sleep(_):
        return None

    monkeypatch.setattr(
        run, "OrderManager", lambda *a, **kw: real_om(*a, sleep=no_sleep, **kw)
    )

    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_position(StockContract("SPY"), 400, 670.0)  # fully invested
    bars = series(650, 0.008)
    last = bars[-1].close
    broker.set_history("XSP", HistoryKind.PRICE, bars)
    broker.set_history(
        "XSP",
        HistoryKind.IMPLIED_VOL,
        [
            Bar(TODAY - timedelta(days=252 - i), v, v, v, v)
            for i, v in enumerate(_ivs())
        ],
    )
    broker.set_history(
        "VIX",
        HistoryKind.PRICE,
        [
            Bar(TODAY - timedelta(days=6 - i), v, v, v, v)
            for i, v in enumerate([22, 23, 24, 25, 24, 23])
        ],
    )
    base = round(last / 10) * 10
    strikes = tuple(float(base - 10 * i) for i in range(1, 12))
    broker.set_chain("XSP", [OptionChain("XSP", "XSP", 100, (EXP,), strikes)])
    for k in strikes:
        m = math.log(last / k)
        delta = -0.5 * math.erfc(m / (0.22 * math.sqrt(38 / 365)) / math.sqrt(2))
        broker.set_quote(fair_quote(xsp(k), last, 0.22, delta, width=0.02))

    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
        )
    )
    asyncio.run(broker.connect())
    shorts = [
        p
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, OptionContract)
    ]
    assert shorts and shorts[0].quantity < 0 and shorts[0].contract.underlying == "XSP"
    j = Journal(tmp_path / "j.db")
    assert j.latest("scan_results") is not None
    assert any(r.data.trigger == "SCAN_READY" for r in j.read("intents"))
    assert j.latest("risk_checks").data["approved"] is True


def test_put_size_respects_notional_caps(tmp_path):
    big = cand(StrategyName.INDEX_PUT, xsp(600), stress=100.0)  # budget alone: 20 (cap)
    # Per-expiry cap: 35% x 50% x $1M = $175k -> 2 contracts of $60k.
    assert (
        size_put(big, 100_000, CFG, notional_room([], 1_000_000, CFG, big.contract))
        == 2
    )
    held = [Position(xsp(600), -2, 3.0)]  # $120k already in this expiry
    assert (
        size_put(big, 100_000, CFG, notional_room(held, 1_000_000, CFG, big.contract))
        == 0
    )
    j = Journal(tmp_path / "j.db")
    [it] = entry_intents(
        ScanResult([big]), BUDGET, CFG, j, TODAY, NOW, positions=[], nlv=1_000_000
    )
    assert it.legs[0].quantity == 2
