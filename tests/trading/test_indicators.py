"""Indicators (plan T29) against reference values."""
import math
from datetime import date, timedelta

import pytest

from src.trading import indicators as ind
from src.trading.domain import Bar

D0 = date(2026, 1, 1)


def bars_from(closes, spread=1.0):
    return [
        Bar(D0 + timedelta(days=i), c, c + spread, c - spread, c)
        for i, c in enumerate(closes)
    ]


def test_sma_and_slope():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1, 2], 3) is None
    rising = list(range(1, 61))
    assert ind.sma_slope(rising, 50) > 0
    assert ind.sma_slope(list(range(60, 0, -1)), 50) < 0


def test_rsi_reference():
    # Classic Wilder example (Welles Wilder / StockCharts): RSI(14) ~ 70.5 on these closes.
    closes = [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
    ]
    assert ind.rsi(closes) == pytest.approx(70.46, abs=0.1)
    assert ind.rsi([1.0] * 20) == 50.0
    assert ind.rsi(list(range(20))) == 100.0
    assert ind.rsi([1, 2]) is None


def test_atr_constant_range():
    assert ind.atr(bars_from([100.0] * 30, spread=2.0)) == pytest.approx(4.0)


def test_realized_vol_matches_formula():
    closes = [
        100 * math.exp(0.01 * ((-1) ** i)) for i in range(61)
    ]  # +-1% daily swings
    rets = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:])]
    mean = sum(rets) / len(rets)
    expected = math.sqrt(
        sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    ) * math.sqrt(252)
    assert ind.realized_vol(closes, 20) == pytest.approx(expected)
    assert ind.realized_vol([100.0] * 10, 20) is None


def test_iv_rank_and_percentile():
    ivs = [0.10 + 0.01 * i for i in range(21)]  # 0.10 .. 0.30
    assert ind.iv_rank(ivs) == pytest.approx(100.0)
    assert ind.iv_rank(ivs, current=0.20) == pytest.approx(50.0)
    assert ind.iv_percentile(ivs, current=0.20) == pytest.approx(100 * 10 / 21)
    assert ind.iv_rank([0.2] * 5) is None


def test_compute_and_derived_signals():
    closes = [100 + 0.5 * i for i in range(220)]  # steady uptrend
    ivs = [0.15 + 0.001 * i for i in range(252)]
    x = ind.compute(bars_from(closes), ivs)
    assert x.sma200 is not None and x.uptrend is True
    assert x.rv20 is not None and x.vol_edge is not None
    assert x.iv_rank == pytest.approx(100.0)
    # A 20-day return of 10.5/... is under 8% here, so not a "strong" uptrend.
    assert x.strong_uptrend(closes) is False
    jump = closes[:-1] + [closes[-1] * 1.12]
    assert ind.compute(bars_from(jump), ivs).strong_uptrend(jump) is True
    assert set(x.to_dict()) >= {"rsi14", "iv_rank", "vol_edge"}


def test_short_history_gives_none():
    x = ind.compute(bars_from([100.0] * 10))
    assert (
        x.sma50 is None and x.rsi14 is None and x.uptrend is None and x.vol_edge is None
    )
    assert ind.compute([]) is None


def test_term_structure_and_skew():
    assert ind.term_structure(0.30, 0.22) == pytest.approx(0.08)  # inverted
    assert ind.put_call_skew(0.24, 0.18) == pytest.approx(0.06)
