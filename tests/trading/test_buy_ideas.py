"""Buy ideas: signals, sizing to the account, long calls, cheap protection."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import buy_ideas, run
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    Greeks,
    HistoryKind,
    Position,
    Quote,
    Right,
    StockContract,
    set_clock,
)
from src.trading import indicators
from src.trading.journal import Journal
from src.trading.external_calls import call_contract

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def uptrend_then_dip(n=260, start=100.0, drop=0.06):
    """A steady climb, then a sharp 5-day drop: a dip in an uptrend."""
    closes = [start * (1 + 0.0015 * i) for i in range(n)]
    top = closes[-6]
    for k in range(5):
        closes[-5 + k] = top * (1 - drop * (k + 1) / 5)
    return [
        Bar(TODAY - timedelta(days=n - i), c, c * 1.005, c * 0.995, c)
        for i, c in enumerate(closes)
    ]


def crash(n=260, start=100.0):
    closes = [start] * (n - 40) + [start * (1 - 0.3 * k / 40) for k in range(1, 41)]
    return [
        Bar(TODAY - timedelta(days=n - i), c, c * 1.005, c * 0.995, c)
        for i, c in enumerate(closes)
    ]


def test_signals():
    bars = uptrend_then_dip()
    why = buy_ideas.signals(bars, indicators.compute(bars))
    assert len(why) == 1 and why[0].startswith("dip in uptrend")
    c = crash()
    why = buy_ideas.signals(c, indicators.compute(c))
    assert any(w.startswith("deep drawdown: -30%") for w in why)
    flat = [Bar(b.day, 100, 100.5, 99.5, 100) for b in bars]
    assert buy_ideas.signals(flat, indicators.compute(flat)) == []


ACCT = AccountSnapshot(
    "DU1", NOW, 1_000_000, 700_000, 300_000, 350_000, 650_000, 400_000
)
TIGHT = AccountSnapshot("DU1", NOW, 1_000_000, 130_000, 870_000, 900_000, 100_000, 0)


def test_size_shares_fits_or_says_why():
    cfg = TradingConfig()
    ok = buy_ideas.size_shares("SPY", 600, [], {}, ACCT, cfg, TODAY, ["dip"])
    assert ok.fits and ok.shares == int(0.05 * 1_000_000 // 600)
    no = buy_ideas.size_shares("SPY", 600, [], {}, TIGHT, cfg, TODAY, ["dip"])
    assert not no.fits and "doesn't fit yet" in no.note and no.shares == 0


def cq(k, delta, ask, exp=TODAY + timedelta(days=300)):
    c = call_contract("SPY", exp, k)
    return Quote(c, ask * 0.97, ask, NOW, underlying_price=600, greeks=Greeks(delta))


def test_long_call_when_options_are_cheap():
    bars = uptrend_then_dip()
    ind = indicators.compute(bars, [0.30] * 200 + [0.12] * 50)  # IV low vs its year
    quotes = [cq(480, 0.85, 140), cq(520, 0.76, 110), cq(560, 0.62, 80)]
    idea = buy_ideas.long_call("SPY", ind, quotes, ACCT, TradingConfig(), ["dip"])
    assert idea.key.endswith("C 520") and idea.cost == 11_000 and idea.fits
    rich = indicators.compute(bars, [0.12] * 200 + [0.30] * 50)
    assert buy_ideas.long_call("SPY", rich, quotes, ACCT, TradingConfig(), []) is None
    tight = buy_ideas.long_call("SPY", ind, quotes, TIGHT, TradingConfig(), [])
    assert not tight.fits and "above the floor" in tight.note


def test_protection_needs_cheap_iv_and_a_real_gain():
    from src.trading.domain import OptionContract

    c = OptionContract("XSP", TODAY + timedelta(days=180), 600.0, Right.PUT)
    put = Quote(c, 4.8, 5.0, NOW, greeks=Greeks(-0.1))
    assert buy_ideas.protection(30, put, 0, 10_000) is None  # IV not cheap
    good = buy_ideas.protection(15, put, 100_000, 103_000)
    assert good.fits and good.cost == 500
    weak = buy_ideas.protection(15, put, 100_000, 100_200)
    assert not weak.fits


def test_engine_builds_ideas_once_a_day_and_the_page_shows_them(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_CACHE_DIR", str(tmp_path / "cache"))
    set_clock(lambda: NOW)
    b = FakeAdapter(account_id="DU1")
    asyncio.run(b.connect())
    b.set_account(
        net_liquidation=1_000_000, excess_liquidity=700_000, maint_margin=300_000
    )
    bars = uptrend_then_dip(start=420.0)
    b.set_history("SPY", HistoryKind.PRICE, bars)
    b.set_history(
        "SPY",
        HistoryKind.IMPLIED_VOL,
        [Bar(x.day, v, v, v, v) for x, v in zip(bars, [0.30] * 200 + [0.12] * 60)],
    )
    price = bars[-1].close
    exp = TODAY + timedelta(days=300)
    strikes = tuple(float(k) for k in range(int(price * 0.7), int(price * 1.1), 5))
    b.set_chain("SPY", [OptionChain("SPY", "SPY", 100, (exp,), strikes)])

    def source(c):
        if getattr(c, "right", None) is not Right.CALL:
            return None
        delta = min(0.95, max(0.05, 0.5 + (price - c.strike) / (price * 0.4)))
        ask = max(1.0, price - c.strike + 25)
        return Quote(
            c, ask * 0.97, ask, NOW, underlying_price=price, greeks=Greeks(delta)
        )

    b.set_quote_source(source)
    from src.trading.executor import Executor

    ex = Executor.__new__(Executor)
    ex.broker = b

    async def quotes(contracts):
        return {q.contract.key: q for q in await b.quotes(contracts)}

    ex.quotes = quotes

    class Ctx:
        history = None

    cfg = TradingConfig.model_validate(
        {"watchlist": {"SPY": {"strategies": ["covered_call"]}}}
    )
    j = Journal(tmp_path / "j.db")
    snap = asyncio.run(b.account_snapshot())
    data = asyncio.run(
        run._buy_ideas(ex, j, cfg, [], snap, Ctx(), TODAY)
    )  # pylint: disable=protected-access
    kinds = {i["kind"] for i in data["ideas"]}
    assert kinds == {"shares", "long_call"}
    assert all(i["fits"] for i in data["ideas"])
    assert (
        asyncio.run(run._buy_ideas(ex, j, cfg, [], snap, Ctx(), TODAY)) is None
    )  # once a day
    lines = run._buy_ideas_lines(
        j, TODAY.isoformat()
    )  # pylint: disable=protected-access
    assert "Buy ideas (you decide)" in lines and "SPY" in lines
    set_clock(None)

    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    html = create_app().test_client().get("/bot/opportunities").get_data(as_text=True)
    assert "Buy ideas" in html and "dip in uptrend" in html and "long call" in html
