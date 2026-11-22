"""Opportunity tracker: when to sell (plan T31, spec §5.10)."""
from datetime import date, timedelta

import pytest

from src.trading.config import StrategyName
from src.trading.domain import Bar
from src.trading.indicators import Indicators
from src.trading.journal import Journal
from src.trading.tracker import FIRE_AT, Tracker

TODAY = date(2026, 11, 23)
PUT, CC, IDX = StrategyName.STOCK_PUT, StrategyName.COVERED_CALL, StrategyName.INDEX_PUT


def ind(ivr=30.0, ivp=40.0, from_high=3.0, from_low=3.0):
    return Indicators(
        100,
        100,
        100,
        100,
        0.001,
        50,
        2.0,
        0.15,
        0.15,
        0.2,
        ivr,
        ivp,
        from_high,
        from_low,
    )


def flat_bars(n=30, last_moves=()):
    closes = [100.0] * n
    for i, m in enumerate(last_moves, start=1):
        closes[-len(last_moves) - 1 + i] = closes[-len(last_moves) - 1 + i - 1] + m
    return [
        Bar(TODAY - timedelta(days=n - i), c, c + 1, c - 1, c)
        for i, c in enumerate(closes)
    ]


@pytest.fixture
def tracker(tmp_path):
    return Tracker(Journal(tmp_path / "j.db"))


def seed(tracker, scores, strategy=PUT, sym="AAPL"):
    for i, s in enumerate(scores):
        day = TODAY - timedelta(days=len(scores) - i)
        tracker.journal.append(
            "scan_results",
            {
                "date": day.isoformat(),
                "best": [
                    {
                        "strategy": strategy.value,
                        "key": f"{sym} 2026-12-31 P 200",
                        "raw_score": s,
                    }
                ],
                "rejected": {},
            },
        )


def test_neutral_does_not_fire(tracker):
    t = tracker.timing(PUT, "AAPL", ind(), flat_bars(), "normal", 1.0, TODAY)
    assert t.multiplier == 1.0 and not t.fire


def test_high_iv_fires(tracker):
    t = tracker.timing(PUT, "AAPL", ind(ivr=55), flat_bars(), "normal", 1.0, TODAY)
    assert t.multiplier == pytest.approx(1.1) and t.fire and "IV high" in t.reasons[0]


def test_direction_of_the_move_matters(tracker):
    drop = flat_bars(last_moves=(-1.5, -1.5))  # ~3 points down vs ATR ~2
    rise = flat_bars(last_moves=(1.5, 1.5))
    assert tracker.timing(PUT, "AAPL", ind(), drop, "normal", None, TODAY).fire
    assert not tracker.timing(PUT, "AAPL", ind(), rise, "normal", None, TODAY).fire
    assert tracker.timing(CC, "QQQM", ind(), rise, "normal", None, TODAY).fire
    assert tracker.timing(
        CC, "QQQM", ind(from_high=0.3), flat_bars(), "normal", None, TODAY
    ).fire


def test_quartiles_from_scan_history(tracker):
    seed(tracker, [float(i) for i in range(1, 11)])  # 10 days, scores 1..10
    top = tracker.timing(PUT, "AAPL", ind(), flat_bars(), "normal", 9.5, TODAY)
    bottom = tracker.timing(PUT, "AAPL", ind(ivr=55), flat_bars(), "normal", 1.0, TODAY)
    assert top.fire and "top quartile" in top.reasons[0]
    assert (
        bottom.multiplier == pytest.approx(1.0) and not bottom.fire
    )  # IV +0.1, bottom -0.1


def test_history_excludes_today_and_other_names(tracker):
    seed(tracker, [5.0] * 6)
    seed(tracker, [99.0] * 6, sym="MSFT")
    tracker.journal.append(
        "scan_results",
        {
            "date": TODAY.isoformat(),
            "best": [{"strategy": "stock_put", "key": "AAPL x", "raw_score": 50}],
        },
    )
    assert tracker.daily_best(PUT, "AAPL", TODAY) == [5.0] * 6


def test_vix_fade_boosts_index_puts_only(tracker):
    assert tracker.timing(
        IDX, "XSP", ind(), flat_bars(), "fading", None, TODAY
    ).multiplier == pytest.approx(1.2)
    assert (
        tracker.timing(
            PUT, "AAPL", ind(), flat_bars(), "fading", None, TODAY
        ).multiplier
        == 1.0
    )


def test_clamped(tracker):
    seed(tracker, [float(i) for i in range(1, 11)], strategy=IDX, sym="XSP")
    t = tracker.timing(
        IDX, "XSP", ind(ivr=90, from_low=0.1), flat_bars(), "fading", 99, TODAY
    )
    assert t.multiplier == 1.3


def test_replay_fires_on_the_rich_day_not_the_first_acceptable(tracker):
    """A month of acceptable-but-ordinary premium, then a rich day: only that one fires."""
    scores = [
        1.0,
        1.1,
        0.9,
        1.0,
        1.05,
        0.95,
        1.0,
        1.1,
        0.9,
        1.0,
        1.0,
        1.05,
        0.95,
        1.0,
        1.1,
        0.9,
        1.0,
        1.0,
        1.05,
        0.95,
    ]
    fired_on = []
    for day_index, score in enumerate(scores + [2.0]):
        day = TODAY + timedelta(days=day_index)
        t = tracker.timing(PUT, "AAPL", ind(), flat_bars(), "normal", score, day)
        if t.fire:
            fired_on.append(day_index)
        tracker.journal.append(
            "scan_results",
            {
                "date": day.isoformat(),
                "best": [
                    {
                        "strategy": "stock_put",
                        "key": "AAPL 2026-12-31 P 200",
                        "raw_score": score,
                    }
                ],
            },
        )
    assert fired_on[-1] == len(scores)  # the rich day fires
    assert 0 not in fired_on  # the first acceptable day did not


def test_scan_marks_ready_with_tracker(tmp_path):
    import asyncio

    from tests.trading.test_scanner import (  # reuse the T30 fixtures
        ACCOUNT,
        EXP,
        NOW,
        OptionChain,
        TradingConfig,
        fair_quote,
        ivs,
        series,
        xsp_put,
    )
    from src.trading.broker.fake import FakeAdapter
    from src.trading.domain import HistoryKind
    from src.trading.history import HistoryStore
    from src.trading.scanner import scan
    import math

    broker = FakeAdapter(account_id="DU1", read_only=True)
    asyncio.run(broker.connect())
    bars = series(650, 0.008)
    broker.set_history("XSP", HistoryKind.PRICE, bars)
    broker.set_history(
        "XSP",
        HistoryKind.IMPLIED_VOL,
        [
            Bar(TODAY - timedelta(days=252 - i), v, v, v, v)
            for i, v in enumerate(ivs(0.22))
        ],
    )
    last = bars[-1].close
    base = round(last / 10) * 10
    strikes = tuple(float(base - 10 * i) for i in range(1, 12))
    broker.set_chain("XSP", [OptionChain("XSP", "XSP", 100, (EXP,), strikes)])
    for k in strikes:
        m = math.log(last / k)
        delta = -0.5 * math.erfc(m / (0.22 * math.sqrt(38 / 365)) / math.sqrt(2))
        broker.set_quote(fair_quote(xsp_put(k), last, 0.22, delta))
    cfg = TradingConfig.model_validate(
        {"enabled_strategies": {"index_put": {"manage": True, "open": True}}}
    )
    journal = Journal(tmp_path / "j.db")
    store = HistoryStore(broker, tmp_path / "cache", clock=lambda: NOW)
    res = asyncio.run(
        scan(
            broker,
            store,
            [],
            ACCOUNT,
            cfg,
            TODAY,
            journal=journal,
            tracker=Tracker(journal),
            regime="fading",
        )
    )
    assert res.ready() and all(c.timing >= FIRE_AT for c in res.ready())
    assert res.best(1)[0].score == pytest.approx(
        res.best(1)[0].raw_score * res.best(1)[0].timing
    )
    no_tracker = asyncio.run(scan(broker, store, [], ACCOUNT, cfg, TODAY))
    assert no_tracker.candidates and no_tracker.ready() == []
