"""Backtester (plan T37): the real engine replayed on synthetic history."""
import asyncio
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import rolls
from src.trading.backtest.data import MarketHistory
from src.trading.backtest.engine import BacktestSettings, Backtester, run_backtest
from src.trading.backtest.report import _max_drawdown, build_report, to_markdown
from src.trading.backtest.synthetic import SyntheticMarket
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    Action,
    Bar,
    ExerciseStyle,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Quote,
    Right,
    StockContract,
    TradeIntent,
    set_clock,
    utcnow,
)
from src.trading.journal import Journal


def make_history(start=date(2022, 1, 3), n=560, seed=5):
    rnd = random.Random(seed)
    h = MarketHistory()
    spx, qqq, vix = [], [], {}
    p, q, v = 4500.0, 380.0, 18.0
    d = start
    while len(spx) < n:
        if d.weekday() < 5:
            shock = rnd.gauss(0, 1)
            v = max(
                11,
                min(
                    60,
                    v
                    * math.exp(
                        -0.064 * shock + 0.05 * (18 - v) / 18 + rnd.gauss(0, 0.04)
                    ),
                ),
            )
            r = shock * v / 100 / math.sqrt(252)
            p *= math.exp(r)
            q *= math.exp(r * 1.2)
            spx.append(Bar(d, p, p * 1.004, p * 0.996, p))
            qqq.append(Bar(d, q, q * 1.005, q * 0.995, q))
            vix[d] = v
        d += timedelta(days=1)
    h.bars["SPX"] = spx
    h.bars["SPY"] = [
        Bar(b.day, b.open / 10, b.high / 10, b.low / 10, b.close / 10) for b in spx
    ]
    h.bars["QQQ"] = qqq
    h.vol["VIX"] = vix
    return h


CFG = TradingConfig.model_validate(
    {
        "enabled_strategies": {
            "index_put": {"manage": True, "open": True},
            "put_spread": {"manage": True, "open": True},
            "covered_call": {"manage": True, "open": True},
        },
        "watchlist": {
            "SPY": {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
            "QQQ": {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
        },
    }
)


@pytest.fixture(scope="module")
def backtest(tmp_path_factory):
    h = make_history()
    days = h.days("SPX")
    settings = BacktestSettings(start=days[300], end=days[345])
    seen = []
    bt = Backtester(SyntheticMarket(h), CFG, settings, tmp_path_factory.mktemp("bt"))

    def progress(i, n, rec):
        # No look-ahead: the engine never saw a bar after the day it ran on.
        latest = max(b.day for b in bt.broker._history[("XSP", bt_kind)])  # noqa
        seen.append((rec.day, latest))

    from src.trading.domain import HistoryKind

    bt_kind = HistoryKind.PRICE
    result = asyncio.run(bt.run(progress))
    return result, seen


def test_runs_every_trading_day_without_look_ahead(backtest):
    result, seen = backtest
    assert len(result.days) == 45  # 46 weekdays, less Good Friday 2023 (no session)
    assert all(latest == day for day, latest in seen)
    assert utcnow().year >= 2026  # the real clock is restored afterwards


def test_the_engine_trades_and_the_account_adds_up(backtest):
    result, _ = backtest
    j = Journal(result.journal_path)
    fills = [r.data for r in j.read("fills")]
    j.close()
    assert any(
        f.contract.underlying == "XSP" and f.action is Action.SELL for f in fills
    )
    first = result.days[0]
    assert first.nlv == pytest.approx(1_000_000, rel=1e-3)
    assert first.baseline_nlv == pytest.approx(first.nlv, rel=1e-6)
    for d in result.days:
        assert d.nlv == pytest.approx(d.cash + d.stock_value + d.options_value, abs=1)
        assert d.excess_liquidity == pytest.approx(d.nlv - d.maint_margin, abs=1)
        assert d.excess_liquidity > 100_000


def test_report(backtest):
    result, _ = backtest
    r = build_report(result)
    assert r.trading_days == 45 and r.trades >= 1 and r.premium_sold > 0
    assert 0 <= r.fees_pct_of_premium < 0.05
    assert r.days_below_floor == 0 and r.halts == []
    md = to_markdown(r, "Test")
    assert "Options contribution" in md and "Win rate" in md
    assert '"win_rate"' in r.to_json()


def test_max_drawdown():
    assert _max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert _max_drawdown([1, 2, 3]) == 0


# -- fake broker pieces the backtest relies on ---------------------------------------------

NOW = datetime(2026, 11, 20, 20, 0, tzinfo=timezone.utc)
EXP = date(2026, 11, 20)


def _xsp(k, right=Right.PUT):
    return OptionContract(
        "XSP", EXP, k, right, 100, ExerciseStyle.EUROPEAN, True, "XSP"
    )


def test_settle_expired():
    f = FakeAdapter(clock=lambda: NOW)
    f.set_account(cash=0.0)
    spy_put = OptionContract("SPY", EXP, 600, Right.PUT)
    f.set_position(_xsp(600), -2, 3.0)  # ITM by 10: pays 2 x 100 x 10
    f.set_position(_xsp(580), -1, 1.0)  # OTM: expires
    f.set_position(spy_put, -1, 2.0)  # ITM: 100 shares at 600
    prices = {"XSP": 590.0, "SPY": 590.0}
    events = f.settle_expired(EXP + timedelta(days=3), lambda u, d: prices[u])
    kinds = {k: (kind, amt) for k, kind, amt in events}
    assert kinds[_xsp(600).key] == ("cash_settled", -2000.0)
    assert kinds[_xsp(580).key][0] == "expired"
    assert kinds[spy_put.key] == ("assigned", -1000.0)
    asyncio.run(f.connect())
    held = {p.contract.key: p.quantity for p in asyncio.run(f.positions())}
    assert held == {"SPY": 100}
    assert f._account.cash == pytest.approx(-2000 - 60_000)  # noqa


def test_fill_threshold_and_combo_leg_prices():
    f = FakeAdapter(clock=lambda: NOW)
    asyncio.run(f.connect())
    short, long_ = _xsp(600), _xsp(595)
    f.set_quote(Quote(short, 2.90, 3.10, NOW))
    f.set_quote(Quote(long_, 1.90, 2.10, NOW))
    combo = (OrderLeg(short, Action.SELL, 1), OrderLeg(long_, Action.BUY, 1))
    # Net mid credit 1.00; natural 0.80. At 0.95 (25% of the way): rests.
    state = asyncio.run(f.place_order(OrderRequest("a", combo, -0.95, "a")))
    assert not state.status.is_done
    f.fill_threshold = 0.25
    state = asyncio.run(f.place_order(OrderRequest("b", combo, -0.95, "b")))
    assert state.status.is_done
    legs = [x for x in f._fills if x.order_ref == "b"]  # noqa
    net = sum(x.action.sign * x.price for x in legs)
    assert net == pytest.approx(-0.95)  # legs add up to the net price


def test_rejected_roll_becomes_a_close():
    old, new = _xsp(600), _xsp(590)
    roll = TradeIntent(
        "r",
        rolls.STRATEGY,
        IntentKind.ROLL,
        (OrderLeg(old, Action.BUY, 3), OrderLeg(new, Action.SELL, 3)),
        "EARLY_ROLL",
        NOW,
        False,
        "chain1",
    )
    plan = rolls.RollPlan("roll", "delta", roll, chain={"chain_id": "chain1"})
    close = rolls.close_instead(plan, "roll rejected: expiry_ladder", EXP)
    assert close.action == "close" and close.intent.risk_reducing
    assert close.intent.legs == (OrderLeg(old, Action.BUY, 3),)
    assert close.intent.chain_id == "chain1"


def test_set_clock():
    set_clock(lambda: NOW)
    try:
        assert utcnow() == NOW
    finally:
        set_clock(None)
    assert utcnow() != NOW
