"""Two simulated weeks of the real engine on a stressed account (recovery profile).

The account starts the way it looks after margin calls: shares bought on
margin plus a book of naked XSP puts, Excess Liquidity under the floor but
above the emergency level. The market rallies, then sells off hard with a
VIX spike, then steadies. Every day the real engine runs one cycle
(backtest harness: margin monitor, restoration, guard and rolls, scanner,
entries, risk gate, order manager) with ``margin_floor.auto`` off.

What must hold all along:

* nothing is bought back by margin restoration (advice alerts instead),
* covered calls are sold on the shares,
* no new short puts or put spreads are opened while the account fails the
  crash test,
* the advice alert goes out, and every day runs.
"""
import asyncio
import math
from datetime import timedelta

import pytest

from src.trading.backtest import engine as bt_engine
from src.trading.backtest.engine import BacktestSettings, Backtester
from src.trading.backtest.synthetic import SyntheticMarket
from src.trading.config import TradingConfig
from src.trading.domain import (
    Action,
    Bar,
    ExerciseStyle,
    IntentKind,
    OptionContract,
    Right,
)
from src.trading.journal import Journal
from tests.trading.test_backtest import make_history

# Daily S&P moves and VIX closes for the 10 days: rally, sell-off, steady.
MOVES = [0.0, 0.005, 0.015, 0.01, -0.03, -0.025, -0.01, 0.01, 0.005, 0.0]
VIX = [18, 17, 16, 16, 26, 33, 35, 30, 26, 24]
PUTS = 40  # naked XSP puts at the start


def scripted_history():
    h = make_history()
    days = h.days("SPX")
    w0 = 300
    window = days[w0 : w0 + len(MOVES)]
    spx = list(h.bars["SPX"][:w0])
    qqq = list(h.bars["QQQ"][:w0])
    p, q = spx[-1].close, qqq[-1].close
    for d, r, v in zip(window, MOVES, VIX):
        p *= math.exp(r)
        q *= math.exp(r * 1.2)
        spx.append(Bar(d, p, p * 1.004, p * 0.996, p))
        qqq.append(Bar(d, q, q * 1.005, q * 0.995, q))
        h.vol["VIX"][d] = v
    h.bars["SPX"] = spx
    h.bars["QQQ"] = qqq
    h.bars["SPY"] = [
        Bar(b.day, b.open / 10, b.high / 10, b.low / 10, b.close / 10) for b in spx
    ]
    h.vol["VIX"] = {d: v for d, v in h.vol["VIX"].items() if d <= window[-1]}
    return h, window


class Recorder:
    sent = []

    def __init__(self, *_a, **_k):
        pass

    def notify(self, level, key, title, message):
        Recorder.sent.append((level, key, title, message))

    async def anotify(self, *a):
        self.notify(*a)


class StressedStart(Backtester):
    """Shares on margin (160% invested) plus naked XSP puts ~8% out of the money."""

    def _start(self, day):
        super()._start(day)
        xsp = self.market.price("XSP", day) or self.market.price("SPY", day)
        strike = math.floor(xsp * 0.92 / 5) * 5
        expiry = next(e for e in sorted(self._expiries(day)) if (e - day).days >= 25)
        put = OptionContract(
            "XSP",
            expiry,
            float(strike),
            Right.PUT,
            style=ExerciseStyle.EUROPEAN,
            cash_settled=True,
        )
        q = self.market.quote(put)
        self.broker.set_position(put, -PUTS, q.mid)
        self.broker._adjust_cash(PUTS * 100 * q.mid)  # pylint: disable=protected-access

    def _expiries(self, day):
        from src.trading.backtest import synthetic

        return synthetic.expiries(day)


def recovery_config(el: float) -> TradingConfig:
    """The repo's recovery profile, scaled to this account's starting EL."""
    return TradingConfig.model_validate(
        {
            "enabled_strategies": {
                "covered_call": {"manage": True, "open": True},
                "index_put": {"manage": True, "open": True},
                "put_spread": {"manage": True, "open": True},
            },
            "watchlist": {
                "SPY": {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
                "QQQ": {"strategies": ["covered_call"], "cc_max_coverage": 0.5},
            },
            "entry": {"cc_all_holdings": True},
            "margin_floor": {
                "excess_liquidity_min": el * 1.3,
                "entry_buffer": el,
                "restore_target": el * 1.5,
                "emergency_level": el * 0.3,
                "cushion_emergency": 0.01,
                "cushion_breach": 0.02,
                "cushion_restore": 0.03,
                "cushion_min_new_trade": 0.35,
                "auto": False,
            },
        }
    )


@pytest.fixture(scope="module")
def week(tmp_path_factory):
    h, window = scripted_history()
    market = SyntheticMarket(h)
    settings = BacktestSettings(
        start=window[0], end=window[-1], holdings={"SPY": 0.8, "QQQ": 0.8}
    )
    bt = StressedStart(market, TradingConfig(), settings, tmp_path_factory.mktemp("wk"))
    # Probe the starting account to scale the floors to it.
    market.day = window[0]
    bt._feed(window[0])  # pylint: disable=protected-access
    bt._start(window[0])  # pylint: disable=protected-access
    start = bt._refresh_account()  # pylint: disable=protected-access
    bt = StressedStart(
        market,
        recovery_config(start.excess_liquidity),
        settings,
        tmp_path_factory.mktemp("wk2"),
    )
    Recorder.sent = []
    mp = pytest.MonkeyPatch()
    mp.setattr(bt_engine, "Notifier", Recorder)
    try:
        result = asyncio.run(bt.run())
    finally:
        mp.undo()
    j = Journal(result.journal_path)
    data = {
        "fills": [r.data for r in j.read("fills")],
        "intents": [r.data for r in j.read("intents")],
        "margin": [r.data for r in j.read("margin_events")],
    }
    j.close()
    return result, data, start, list(Recorder.sent)


def test_every_day_runs_and_the_account_was_stressed(week):
    result, data, start, _ = week
    assert len(result.days) == len(MOVES)
    # Started in breach (below the floor) but above the emergency level.
    states = [m.get("state") for m in data["margin"]]
    assert "breach" in states or "warning" in states, states
    assert "emergency" not in states
    assert result.days[0].excess_liquidity < start.excess_liquidity * 1.3


def test_no_restoration_buy_backs_only_advice(week):
    _, data, _, alerts = week
    assert not [i for i in data["intents"] if i.trigger == "MARGIN_RESTORATION"]
    assert any(a[1] == "restoration-advice" for a in alerts)


def test_covered_calls_sold_and_no_new_puts(week):
    _, data, _, _ = week
    calls = [
        f
        for f in data["fills"]
        if isinstance(f.contract, OptionContract)
        and f.contract.right is Right.CALL
        and f.action is Action.SELL
    ]
    assert calls, "no covered calls sold during the recovery"
    assert {f.contract.underlying for f in calls} <= {"SPY", "QQQ"}
    opened_puts = [
        i
        for i in data["intents"]
        if i.kind is IntentKind.OPEN
        and i.strategy in ("index_put", "put_spread", "stock_put")
    ]
    assert not opened_puts
