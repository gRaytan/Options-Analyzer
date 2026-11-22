"""Opportunity scanner (plan T30, spec §5.9)."""
import asyncio
import math
from datetime import date, datetime, timedelta, timezone

import pytest
from py_vollib.black_scholes import black_scholes

from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import StrategyName, TradingConfig
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
from src.trading.history import HistoryStore
from src.trading.indicators import compute
from src.trading.journal import Journal
from src.trading.scanner import (
    candidate_contracts,
    evaluate,
    scan,
    uncovered_lots,
)

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
ACCOUNT = AccountSnapshot("DU1", NOW, 1_000_000, 700_000, 300_000, 300_000, 650_000)
EXP = TODAY + timedelta(days=38)


def series(start_price, daily_vol, n=260, seed=7, drift=0.0):
    import random

    rnd = random.Random(seed)
    price, bars = start_price, []
    for i in range(n):
        price *= math.exp(drift + rnd.gauss(0, daily_vol))
        bars.append(
            Bar(
                TODAY - timedelta(days=n - i),
                price,
                price * 1.005,
                price * 0.995,
                price,
            )
        )
    return bars


def ivs(current, n=252):
    # A year of IV between 0.10 and 0.30, ending at `current` (high IV rank).
    return [0.10 + 0.20 * (i % 50) / 49 for i in range(n - 1)] + [current]


def xsp_put(strike, expiry=EXP):
    return OptionContract(
        "XSP",
        expiry,
        strike,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
        trading_class="XSP",
    )


def fair_quote(c, und, iv, delta, width=0.04):
    t = (c.expiry - TODAY).days / 365
    mid = black_scholes(
        "p" if c.right is Right.PUT else "c", und, c.strike, t, 0.04, iv
    )
    return Quote(
        c,
        round(mid - width / 2, 2),
        round(mid + width / 2, 2),
        NOW,
        underlying_price=und,
        greeks=Greeks(delta, iv=iv),
    )


def indicators_for(price=650.0, rv_daily=0.008, iv=0.22):
    bars = series(price, rv_daily)
    return compute(bars, ivs(iv))


CFG = TradingConfig()


def test_evaluate_accepts_rich_otm_index_put():
    ind = indicators_for()
    c = xsp_put(600)
    q = fair_quote(c, 650, 0.22, -0.13)
    cand, reason = evaluate(StrategyName.INDEX_PUT, c, q, ind, ACCOUNT, CFG, TODAY)
    assert reason is None, reason
    assert cand.ev > 0 and cand.stress_loss > 0 and cand.score > 0
    assert cand.net_credit == pytest.approx(
        cand.credit
        - 2 * 1.15  # one way: $1 order minimum + $0.10 XSP + $0.05 regulatory
    )  # XSP fees 0.65+0.10+... one way x2


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (dict(delta=-0.30), "delta outside the band"),
        (dict(width=0.60), "spread too wide"),
        # Priced at 12% IV while the index realizes ~14%: credit > $20 but no edge.
        (
            dict(iv=0.12, delta=-0.15, width=0.02, strike=630),
            "no positive expected value",
        ),
    ],
)
def test_evaluate_rejections(mutate, reason):
    ind = indicators_for()
    kw = dict(iv=0.22, delta=-0.13, width=0.04, strike=600)
    kw.update(mutate)
    c = xsp_put(kw["strike"])
    q = fair_quote(c, 650, kw["iv"], kw["delta"], kw["width"])
    cand, why = evaluate(StrategyName.INDEX_PUT, c, q, ind, ACCOUNT, CFG, TODAY)
    assert cand is None and why == reason


def test_options_not_rich_vs_realized():
    ind = indicators_for(rv_daily=0.02, iv=0.22)  # RV ~32% > IV 22%
    c = xsp_put(600)
    cand, why = evaluate(
        StrategyName.INDEX_PUT,
        c,
        fair_quote(c, 650, 0.22, -0.13),
        ind,
        ACCOUNT,
        CFG,
        TODAY,
    )
    assert why == "options not rich vs realized moves"


def test_iv_rank_and_dte_filters():
    low_rank = compute(series(650, 0.008), [0.30] * 251 + [0.12])
    c = xsp_put(600)
    q = fair_quote(c, 650, 0.22, -0.13)
    assert evaluate(StrategyName.INDEX_PUT, c, q, low_rank, ACCOUNT, CFG, TODAY)[1] in (
        "IV rank too low",
        "options not rich vs realized moves",
    )
    far = xsp_put(600, TODAY + timedelta(days=90))
    assert (
        evaluate(
            StrategyName.INDEX_PUT,
            far,
            fair_quote(far, 650, 0.22, -0.13),
            indicators_for(),
            ACCOUNT,
            CFG,
            TODAY,
        )[1]
        == "outside the DTE window"
    )


def test_covered_call_cost_basis_and_qqqm_band():
    cfg = TradingConfig.model_validate(
        {
            "watchlist": {
                "QQQM": {
                    "strategies": ["covered_call"],
                    "cc_delta": [0.06, 0.10],
                    "min_oi": 50,
                    "max_spread_pct": 0.20,
                }
            }
        }
    )
    ind = indicators_for(250, 0.008, 0.22)
    call = OptionContract("QQQM", EXP, 270, Right.CALL)
    q = fair_quote(call, 250, 0.22, 0.09, width=0.06)
    cand, why = evaluate(
        StrategyName.COVERED_CALL, call, q, ind, ACCOUNT, cfg, TODAY, 3, cost_basis=180
    )
    assert why is None, why
    assert (
        evaluate(
            StrategyName.COVERED_CALL,
            call,
            q,
            ind,
            ACCOUNT,
            cfg,
            TODAY,
            3,
            cost_basis=280,
        )[1]
        == "strike below cost basis"
    )
    q15 = fair_quote(call, 250, 0.22, 0.14, width=0.06)
    assert (
        evaluate(
            StrategyName.COVERED_CALL, call, q15, ind, ACCOUNT, cfg, TODAY, 3, 180
        )[1]
        == "delta outside the band"
    )  # QQQM band is 0.06-0.10


def test_uncovered_lots_respects_cap_and_existing_calls():
    cfg = TradingConfig.model_validate(
        {"watchlist": {"QQQM": {"strategies": ["covered_call"]}}}
    )
    positions = [
        Position(StockContract("QQQM"), 650, 180.0, 250.0),  # 50% cap -> 3 lots
        Position(OptionContract("QQQM", EXP, 270, Right.CALL), -1, 1.0),
        Position(StockContract("TSLA"), 500, 1.0),  # not on the watchlist
    ]
    assert uncovered_lots(positions, cfg) == {"QQQM": 2}


def test_candidate_contracts_otm_and_window():
    chain = OptionChain(
        "XSP",
        "XSP",
        100,
        (TODAY + timedelta(days=10), EXP, TODAY + timedelta(days=80)),
        tuple(float(k) for k in range(560, 700, 10)),
    )
    puts = candidate_contracts("XSP", Right.PUT, [chain], 650, TODAY, 30, 45, "XSP")
    assert {c.expiry for c in puts} == {EXP}
    assert max(c.strike for c in puts) == 640 and all(c.cash_settled for c in puts)


def test_scan_end_to_end(tmp_path):
    broker = FakeAdapter(account_id="DU1", read_only=True)
    asyncio.run(broker.connect())
    broker.set_history("XSP", HistoryKind.PRICE, series(650, 0.008))
    broker.set_history(
        "XSP",
        HistoryKind.IMPLIED_VOL,
        [
            Bar(TODAY - timedelta(days=252 - i), v, v, v, v)
            for i, v in enumerate(ivs(0.22))
        ],
    )
    last = series(650, 0.008)[-1].close
    base = round(last / 10) * 10  # strikes around the final price of the random path
    strikes = tuple(float(base - 10 * i) for i in range(1, 12))
    broker.set_chain("XSP", [OptionChain("XSP", "XSP", 100, (EXP,), strikes)])
    for k in strikes:
        c = xsp_put(k)
        m = math.log(last / k)
        delta = -0.5 * math.erfc(m / (0.22 * math.sqrt(38 / 365)) / math.sqrt(2))
        broker.set_quote(fair_quote(c, last, 0.22, delta))
    cfg = TradingConfig.model_validate(
        {"enabled_strategies": {"index_put": {"manage": True, "open": True}}}
    )
    journal = Journal(tmp_path / "j.db")
    store = HistoryStore(broker, tmp_path / "cache", clock=lambda: NOW)
    result = asyncio.run(scan(broker, store, [], ACCOUNT, cfg, TODAY, journal=journal))
    assert result.candidates, result.rejected
    best = result.best(3)
    assert all(c.strategy is StrategyName.INDEX_PUT for c in best)
    assert best == sorted(best, key=lambda c: -c.score)
    assert journal.latest("scan_results").data["best"][0]["key"].startswith("XSP")
    assert "delta outside the band" in result.rejected
