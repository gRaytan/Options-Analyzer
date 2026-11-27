"""Spike watch: real-time triggers between scans."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

from src.trading import run, spikes
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    Bar,
    Greeks,
    HistoryKind,
    Position,
    Quote,
    Right,
    StockContract,
    set_clock,
)
from src.trading.journal import Journal

NOW = datetime(2026, 11, 23, 18, 0, tzinfo=timezone.utc)  # Monday 13:00 ET
TODAY = NOW.date()


def test_stock_spikes_by_atr():
    [up] = spikes.stock_spikes("SPY", 612, 600, 10, want_up=True, want_down=True)
    assert up.kind == spikes.UP and up.size == 1.2 and "+2.0%" in up.detail
    [down] = spikes.stock_spikes("SPY", 588, 600, 10, want_up=True, want_down=True)
    assert down.kind == spikes.DOWN
    assert spikes.stock_spikes("SPY", 605, 600, 10, True, True) == []  # 0.5 ATR
    assert spikes.stock_spikes("SPY", 612, 600, 10, False, True) == []  # calls off
    assert spikes.stock_spikes("SPY", None, 600, 10, True, True) == []


def test_vix_jump_and_fade():
    [jump] = spikes.vix_spikes(22.0, 19.0, 22.0)
    assert jump.kind == spikes.VIX_UP
    kinds = {s.kind for s in spikes.vix_spikes(23.0, 19.0, 25.0)}
    assert kinds == {spikes.VIX_UP, spikes.VIX_FADING}  # still up, 8% off its high
    assert spikes.vix_spikes(19.5, 19.0, 19.8) == []  # small day


def test_cooldown(tmp_path):
    w = spikes.SpikeWatch(Journal(tmp_path / "j.db"))
    s = spikes.stock_spikes("SPY", 612, 600, 10, True, False)
    assert w.new(s, NOW) == s
    assert w.new(s, NOW + timedelta(minutes=10)) == []
    assert w.new(s, NOW + timedelta(minutes=31)) == s
    assert w.vix_high(TODAY, 20.0) == 20.0 and w.vix_high(TODAY, 18.0) == 20.0
    assert w.vix_high(TODAY + timedelta(days=1), 18.0) == 18.0


def bars(prev_close, n=30, rng=10.0):
    out = []
    for i in range(n):
        d = TODAY - timedelta(days=n - i)
        out.append(
            Bar(d, prev_close, prev_close + rng / 2, prev_close - rng / 2, prev_close)
        )
    return out


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, level, key, title, message):
        self.sent.append((level, key, title, message))

    async def anotify(self, *a):
        self.notify(*a)


class Ctx:
    history = None
    spikes = None


def engine(tmp_path, monkeypatch, spy_last, meta_last, vix_last=18.0):
    (tmp_path / "ext.json").write_text(
        json.dumps({"META": {"shares": 1000, "cc_calls": 6, "cc_delta": 0.15}})
    )
    monkeypatch.setenv("TRADING_EXTERNAL_HOLDINGS", str(tmp_path / "ext.json"))
    monkeypatch.setenv("TRADING_CACHE_DIR", str(tmp_path / "cache"))
    set_clock(lambda: NOW)
    b = FakeAdapter(account_id="DU1")
    asyncio.run(b.connect())
    b.set_position(StockContract("SPY"), 400, 500.0)
    b.set_history("SPY", HistoryKind.PRICE, bars(600.0))
    b.set_history("META", HistoryKind.PRICE, bars(600.0, rng=12.0))
    b.set_history("VIX", HistoryKind.PRICE, bars(18.0, rng=1.0))
    b.set_quote(Quote(StockContract("SPY"), spy_last, spy_last, NOW, last=spy_last))
    b.set_quote(Quote(StockContract("META"), meta_last, meta_last, NOW, last=meta_last))
    b.set_quote(Quote(StockContract("VIX"), vix_last, vix_last, NOW, last=vix_last))
    exp = TODAY + timedelta(days=36)
    strikes = tuple(float(k) for k in range(620, 820, 5))
    b.set_chain("META", [OptionChain("META", "META", 100, (exp,), strikes)])

    def source(c):
        if getattr(c, "right", None) is not Right.CALL:
            return None
        delta = max(0.02, 0.5 - (c.strike - meta_last) / 220)
        bid = max(0.2, 20 - (c.strike - meta_last) * 0.15)
        return Quote(
            c,
            bid,
            bid * 1.05,
            NOW,
            underlying_price=meta_last,
            greeks=Greeks(delta, iv=0.4),
        )

    b.set_quote_source(source)
    from src.trading.executor import Executor

    ex = Executor.__new__(Executor)
    ex.broker = b

    async def quotes(contracts):
        return {q.contract.key: q for q in await b.quotes(contracts)}

    ex.quotes = quotes
    cfg = TradingConfig.model_validate(
        {
            "enabled_strategies": {"covered_call": {"manage": True, "open": True}},
            "watchlist": {"SPY": {"strategies": ["covered_call"]}},
        }
    )
    j = Journal(tmp_path / "j.db")
    n = Notes()
    scan = asyncio.run(
        run._spike_watch(ex, j, cfg, Ctx(), n)
    )  # pylint: disable=protected-access
    set_clock(None)
    return scan, n.sent, j


def test_quiet_market_does_nothing(tmp_path, monkeypatch):
    scan, sent, j = engine(tmp_path, monkeypatch, spy_last=603.0, meta_last=604.0)
    assert scan is False and sent == [] and j.read("spike_events") == []


def test_spy_rally_scans_now(tmp_path, monkeypatch):
    scan, sent, _ = engine(tmp_path, monkeypatch, spy_last=615.0, meta_last=604.0)
    assert scan is True
    assert any(s[2] == "Spike: SPY (up)" for s in sent)


def test_meta_spike_sends_the_call_to_sell_now(tmp_path, monkeypatch):
    scan, sent, j = engine(tmp_path, monkeypatch, spy_last=603.0, meta_last=618.0)
    assert scan is False  # nothing to trade in this account
    [alert] = [s for s in sent if s[2].startswith("Sell now at your other broker")]
    assert "META +3.0% today" in alert[3] and "Sell 6 META" in alert[3]
    rec = j.latest("job_runs", key="external_calls").data
    assert rec["spike"]["symbol"] == "META"


def test_page_lists_spikes(tmp_path, monkeypatch):
    from src.web.app import create_app

    j = Journal(tmp_path / "j.db")
    w = spikes.SpikeWatch(j)
    w.new(spikes.stock_spikes("SPY", 612, 600, 10, True, False), NOW)
    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    html = create_app().test_client().get("/bot/premium").get_data(as_text=True)
    assert "Recent spikes" in html and "SPY +2.0% today" in html
