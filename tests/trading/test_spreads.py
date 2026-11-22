"""Index put credit spreads (plan T34, spec §5.4)."""
import asyncio
import math
from datetime import timedelta

import pytest

from src.trading import run, spreads
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import StrategyName, TradingConfig
from src.trading.domain import (
    Action,
    Bar,
    HistoryKind,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.entries import entry_intents, size_put, spread_mode
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from, net_put_notional
from src.trading.scanner import (
    ScanResult,
    best_spreads,
    candidate_contracts,
    evaluate_spread,
    thin,
)
from tests.trading.test_scanner import (
    ACCOUNT,
    EXP,
    NOW,
    TODAY,
    fair_quote,
    indicators_for,
    series,
    xsp_put,
)

CFG = TradingConfig()
IND = indicators_for()


def q(k, iv=0.22, und=650.0, width=0.02):
    t = (EXP - TODAY).days / 365
    d1 = (math.log(und / k) + (0.04 + iv * iv / 2) * t) / (iv * math.sqrt(t))
    delta = -0.5 * math.erfc(d1 / math.sqrt(2))
    return fair_quote(xsp_put(k), und, iv, delta, width=width)


# -- scanner -------------------------------------------------------------------


def test_evaluate_spread_accepts_and_caps_risk_at_width():
    cand, reason = evaluate_spread(q(605), q(600), IND, ACCOUNT, CFG, TODAY)
    assert reason is None, reason
    assert cand.strategy is StrategyName.PUT_SPREAD and cand.is_spread
    assert cand.width == 5 and cand.long_contract.strike == 600
    assert 0 < cand.stress_loss <= cand.max_loss < 500
    assert cand.credit == pytest.approx((q(605).mid - q(600).mid) * 100)
    assert cand.ev > 0


@pytest.mark.parametrize(
    "short,long,reason",
    [
        (605, 610, "not a put spread"),
        (605, 570, "spread too wide"),  # $35 > $25
        (640, 635, "delta outside the band"),
    ],
)
def test_evaluate_spread_rejects(short, long, reason):
    assert evaluate_spread(q(short), q(long), IND, ACCOUNT, CFG, TODAY)[1] == reason


def test_credit_floor_vs_width():
    cfg = TradingConfig.model_validate({"entry": {"spread_min_credit_width": 0.20}})
    # Credit/width is about the short delta (~0.13 here): 20% is out of reach.
    assert (
        evaluate_spread(q(605), q(600), IND, ACCOUNT, cfg, TODAY)[1]
        == "credit too small vs the width"
    )


def test_best_spreads_one_per_short_strike_and_fading_band():
    quotes = [q(k) for k in (610, 605, 600, 595, 590, 585, 580)]
    out = best_spreads(quotes, IND, ACCOUNT, CFG, TODAY, "elevated", ScanResult())
    shorts = sorted(c.contract.strike for c in out)
    assert shorts and all(600 <= k <= 610 for k in shorts)  # delta 0.10-0.16
    assert all(c.long_contract.strike < c.contract.strike for c in out)
    fading = best_spreads(quotes, IND, ACCOUNT, CFG, TODAY, "fading", ScanResult())
    assert fading and all(abs(c.quote.greeks.delta) <= 0.10 for c in fading)


def test_candidate_contracts_by_moneyness():
    # $1 strikes from 500 to 649: the 12 nearest would all be ~1-2% OTM.
    chain = OptionChain(
        "XSP", "XSP", 100, (EXP,), tuple(float(k) for k in range(500, 650))
    )
    puts = candidate_contracts(
        "XSP", Right.PUT, [chain], 650, TODAY, 30, 45, "XSP", 20, sigma=0.22
    )
    strikes = sorted(c.strike for c in puts)
    assert len(strikes) <= 20 and all(k % 5 == 0 for k in strikes)
    assert min(strikes) < 600 < max(strikes) < 645  # the 0.05-0.16 delta zone


def test_thin_prefers_round_strikes():
    assert thin([1.0, 2.0, 3.0], 5) == [1.0, 2.0, 3.0]
    assert thin([float(k) for k in range(100, 131)], 7) == [
        100.0,
        105.0,
        110.0,
        115.0,
        120.0,
        125.0,
        130.0,
    ]


# -- caps ------------------------------------------------------------------------


def test_spread_counts_at_its_width():
    short, long_ = xsp_put(600), xsp_put(595)
    held = [Position(short, -10, 3.0), Position(long_, 10, 2.5)]
    [(_, n)] = net_put_notional(exposures_from(held, {}))
    assert n == pytest.approx(5 * 100 * 10)
    # Only 6 longs: 6 at the width, 4 at the full strike.
    held = [Position(short, -10, 3.0), Position(long_, 6, 2.5)]
    [(_, n)] = net_put_notional(exposures_from(held, {}))
    assert n == pytest.approx(5 * 100 * 6 + 600 * 100 * 4)
    # A long at a higher strike, or another expiry, doesn't cap the short.
    other = OptionContract("XSP", EXP + timedelta(days=7), 590, Right.PUT)
    held = [Position(short, -1, 3.0), Position(xsp_put(605), 1, 3.5)]
    held += [Position(other, 1, 1.0)]
    [(_, n)] = net_put_notional(exposures_from(held, {}))
    assert n == pytest.approx(60_000)


# -- entries ---------------------------------------------------------------------

BUDGET = {"free": 100_000.0, "multiplier": 0.6, "regime": "fading"}


def ready(cand):
    from dataclasses import replace

    return replace(cand, fire=True, timing=1.2)


def test_spread_mode():
    assert spread_mode({"spreads_only": True}, 0.70, CFG)
    assert spread_mode({"spreads_only": False}, 0.45, CFG)  # Cushion 40-50%
    assert not spread_mode({"spreads_only": False}, 0.70, CFG)
    assert not spread_mode(None, None, CFG)


def test_spread_sized_by_max_loss(tmp_path):
    cand, _ = evaluate_spread(q(605), q(600), IND, ACCOUNT, CFG, TODAY)
    # 1% of $1M = $10k of max loss; the XSP cap is 20 contracts.
    expected = min(20, int(10_000 // cand.max_loss), int(100_000 // cand.stress_loss))
    assert size_put(cand, 100_000, CFG, None, 1_000_000) == expected
    assert size_put(cand, 100_000, CFG, None, 100_000) == int(1_000 // cand.max_loss)


def test_entries_pick_spreads_or_naked_by_mode(tmp_path):
    from src.trading.scanner import evaluate

    spread, _ = evaluate_spread(q(605), q(600), IND, ACCOUNT, CFG, TODAY)
    naked, _ = evaluate(
        StrategyName.INDEX_PUT, xsp_put(605), q(605), IND, ACCOUNT, CFG, TODAY
    )
    result = ScanResult([ready(spread), ready(naked)])
    j = Journal(tmp_path / "j.db")
    kw = dict(positions=[], nlv=1_000_000)

    [it] = entry_intents(
        result, {**BUDGET, "spreads_only": True}, CFG, j, TODAY, NOW, **kw
    )
    assert it.strategy == "put_spread" and len(it.legs) == 2
    sell, buy = it.legs
    assert (sell.action, sell.contract.strike) == (Action.SELL, 605)
    assert (buy.action, buy.contract.strike) == (Action.BUY, 600)
    assert sell.quantity == buy.quantity > 0
    assert it.details["worst_price"] < 0 and it.details["width"] == 5

    calm = {**BUDGET, "spreads_only": False}
    [it] = entry_intents(result, calm, CFG, j, TODAY, NOW, **kw, excess_liquidity=7e5)
    assert it.strategy == "index_put" and len(it.legs) == 1
    [it] = entry_intents(result, calm, CFG, j, TODAY, NOW, **kw, excess_liquidity=45e4)
    assert it.strategy == "put_spread"  # Cushion 45%: spreads, not naked


# -- management --------------------------------------------------------------------

SHORT, LONG = xsp_put(605), xsp_put(600)
BOOK = [
    Position(StockContract("SPY"), 400, 600.0, 670.0),
    Position(SHORT, -5, 3.2),
    Position(LONG, 5, 2.5),
]


def test_pair_spreads_managed_only():
    assert spreads.pair_spreads(BOOK) == {SHORT.key: BOOK[2]}
    assert spreads.pair_spreads(BOOK, managed={SHORT.key}) == {}  # owner's long
    fewer = [BOOK[1], Position(LONG, 3, 2.5)]
    assert spreads.pair_spreads(fewer) == {}  # doesn't cap all 5


def test_buy_back_becomes_combo_close():
    one = TradeIntent(
        "i",
        "position_manager",
        IntentKind.CLOSE,
        (OrderLeg(SHORT, Action.BUY, 5),),
        "STOP_LOSS",
        NOW,
        True,
    )
    combo = spreads.as_spread_close(one, spreads.pair_spreads(BOOK))
    assert [(l.contract.key, l.action, l.quantity) for l in combo.legs] == [
        (SHORT.key, Action.BUY, 5),
        (LONG.key, Action.SELL, 5),
    ]
    assert combo.risk_reducing and combo.intent_id == "i"
    assert spreads.as_spread_close(one, {}) is one


# -- engine round trip ---------------------------------------------------------------


def _engine(tmp_path, monkeypatch, broker):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    monkeypatch.setattr(
        run,
        "load_trading_config",
        lambda: TradingConfig.model_validate(
            {"enabled_strategies": {"put_spread": {"manage": True, "open": True}}}
        ),
    )
    real_om = run.OrderManager

    async def no_sleep(_):
        return None

    monkeypatch.setattr(
        run, "OrderManager", lambda *a, **kw: real_om(*a, sleep=no_sleep, **kw)
    )

    def once():
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
            )
        )

    return once


def options_held(broker):
    asyncio.run(broker.connect())
    return {
        p.contract.strike: p.quantity
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, OptionContract) and p.quantity != 0
    }


def test_engine_spread_round_trip_in_fading_vix(tmp_path, monkeypatch):
    """VIX spike fading -> spreads only: the bot opens a put spread as one combo,
    then closes both legs together (one combo) when the market drops: the guard's
    roll trigger closes a spread instead of rolling its short leg alone."""
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_position(StockContract("SPY"), 400, 670.0)
    bars = series(650, 0.008)
    last = bars[-1].close
    broker.set_history("XSP", HistoryKind.PRICE, bars)
    ivs = [0.10 + 0.20 * (i % 50) / 49 for i in range(251)] + [0.24]
    broker.set_history(
        "XSP",
        HistoryKind.IMPLIED_VOL,
        [Bar(TODAY - timedelta(days=252 - i), v, v, v, v) for i, v in enumerate(ivs)],
    )
    vix = [36, 40, 42, 41, 38, 34]  # > 30 and 19% below the 5-day high: fading
    broker.set_history(
        "VIX",
        HistoryKind.PRICE,
        [Bar(TODAY - timedelta(days=6 - i), v, v, v, v) for i, v in enumerate(vix)],
    )
    base = round(last / 5) * 5
    strikes = tuple(float(base - 5 * i) for i in range(1, 26))
    broker.set_chain("XSP", [OptionChain("XSP", "XSP", 100, (EXP,), strikes)])
    for k in strikes:
        broker.set_quote(q(k, iv=0.24, und=last))
    once = _engine(tmp_path, monkeypatch, broker)

    once()
    held = options_held(broker)
    assert len(held) == 2, held
    (k_long, n_long), (k_short, n_short) = sorted(held.items())
    assert k_long < k_short and n_short < 0 < n_long and n_long == -n_short
    j = Journal(tmp_path / "j.db")
    opened = [r.data for r in j.read("intents") if r.data.trigger == "SCAN_READY"]
    assert opened and opened[-1].strategy == "put_spread"
    assert j.latest("risk_checks").data["approved"] is True

    # The market drops 7%: the short leg's delta passes 0.35 (roll trigger).
    for k in strikes:
        broker.set_quote(q(k, iv=0.40, und=last * 0.93))
    once()
    assert options_held(broker) == {}
    closes = [r.data for r in Journal(tmp_path / "j.db").read("intents")]
    close = [i for i in closes if i.kind is IntentKind.CLOSE][-1]
    assert len(close.legs) == 2 and close.details.get("spread_long")


def test_stale_roll_plans_are_dropped_when_signals_clear():
    run._last_roll_plans["old"] = object()
    asyncio.run(run._plan_rolls(None, [], [], CFG, None, None, None, TODAY))
    assert not run._last_roll_plans
