"""Covered calls at another broker (recommend + watch) and the monthly income goal."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import external_calls as xc
from src.trading import metrics, run
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import Greeks, Quote, Right, StockContract, set_clock, utcnow
from src.trading.journal import Journal

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
HOLD = xc.Holding("META", 1000, cc_calls=6, cc_delta=0.15, roll_delta=0.35)
EXP = TODAY + timedelta(days=36)


def call_quote(k, delta, bid, und=596.0, exp=EXP):
    c = xc.call_contract("META", exp, k)
    return Quote(
        c,
        bid,
        round(bid * 1.05, 2),
        utcnow(),
        underlying_price=und,
        greeks=Greeks(delta, iv=0.4),
    )


def sale(k, contracts=6, price=5.0, exp=EXP, on=TODAY):
    return {
        "action": "sell",
        "symbol": "META",
        "expiry": exp.isoformat(),
        "strike": k,
        "contracts": contracts,
        "price": price,
        "date": on.isoformat(),
        "trade_id": xc.record_id("META", exp, k, on),
    }


def test_holdings_from_file_data():
    hs = xc.holdings_from(
        {
            "_comment": "x",
            "META": {"shares": 1000, "cc_calls": 6},
            "AAPL": {"shares": 50},
        }
    )
    assert [(h.symbol, h.cc_calls, h.max_calls) for h in hs] == [
        ("META", 6, 10),
        ("AAPL", 0, 0),
    ]


def test_recommend_picks_target_delta_for_missing_contracts():
    quotes = [
        call_quote(660, 0.22, 9.0),
        call_quote(685, 0.16, 5.4),
        call_quote(710, 0.10, 3.3),
    ]
    r = xc.recommend(HOLD, [], quotes)
    assert (
        r.contracts == 6
        and r.contract.strike == 685
        and r.premium == pytest.approx(5.4 * 600)
    )
    opened = xc.open_calls([sale(685, contracts=4)], TODAY)
    assert xc.recommend(HOLD, opened, quotes).contracts == 2
    full = xc.open_calls([sale(685, contracts=6)], TODAY)
    assert xc.recommend(HOLD, full, quotes).contracts == 0


def test_open_calls_track_closes_and_expiry():
    s1, s2 = sale(685), sale(
        700, exp=TODAY - timedelta(days=1), on=TODAY - timedelta(days=40)
    )
    closed = {
        "action": "buy",
        "trade_id": s1["trade_id"],
        "price": 1.0,
        "date": TODAY.isoformat(),
    }
    assert [c.contract.strike for c in xc.open_calls([s1, s2], TODAY)] == [685]
    assert xc.open_calls([s1, s2, closed], TODAY) == []


def test_roll_alerts():
    open_ = xc.open_calls([sale(685)], TODAY)
    key = open_[0].contract.key
    assert xc.roll_alerts(HOLD, open_, {key: call_quote(685, 0.20, 4.0)}, TODAY) == []
    [a] = xc.roll_alerts(HOLD, open_, {key: call_quote(685, 0.41, 14.0)}, TODAY)
    assert "delta 0.41" in a.reason
    near = xc.open_calls([sale(600, exp=TODAY + timedelta(days=3))], TODAY)
    q = call_quote(600, 0.30, 4.0, und=605.0, exp=TODAY + timedelta(days=3))
    [b] = xc.roll_alerts(HOLD, near, {near[0].contract.key: q}, TODAY)
    assert "in the money with 3 days left" in b.reason


def test_income_by_month_includes_other_broker():
    months = {"2026-11": {"premium": 3000.0, "bought": 500.0, "fees": 20.0, "pnl": 0.0}}
    s = sale(685, contracts=6, price=5.0)
    bb = {
        "action": "buy",
        "trade_id": s["trade_id"],
        "price": 1.0,
        "date": "2026-12-03",
    }
    inc = metrics.income_by_month(months, [s, bb])
    assert inc["2026-11"] == pytest.approx(3000 - 500 - 20 + 3000)
    assert inc["2026-12"] == pytest.approx(-600)


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, level, key, title, message):
        self.sent.append((level, key, title, message))

    async def anotify(self, *a):
        self.notify(*a)


def test_engine_recommends_daily_and_alerts_rolls(tmp_path, monkeypatch):
    (tmp_path / "ext.json").write_text(
        json.dumps({"META": {"shares": 1000, "cc_calls": 6, "cc_delta": 0.15}})
    )
    monkeypatch.setenv("TRADING_EXTERNAL_HOLDINGS", str(tmp_path / "ext.json"))
    set_clock(lambda: NOW)
    broker = FakeAdapter(account_id="U1")
    broker.set_quote(Quote(StockContract("META"), 595.9, 596.1, NOW, last=596.0))
    strikes = tuple(float(k) for k in range(600, 820, 5))
    broker.set_chain(
        "META",
        [OptionChain("META", "META", 100, (EXP, TODAY + timedelta(days=60)), strikes)],
    )

    def source(c):
        if getattr(c, "right", None) is not Right.CALL:
            return None
        delta = max(0.02, 0.5 - (c.strike - 596) / 220)
        bid = max(0.2, 20 - (c.strike - 596) * 0.15)
        return Quote(
            c,
            bid,
            bid * 1.05,
            NOW,
            underlying_price=596.0,
            greeks=Greeks(delta, iv=0.4),
        )

    broker.set_quote_source(source)
    asyncio.run(broker.connect())
    j = Journal(tmp_path / "j.db")
    from src.trading.executor import Executor

    ex = Executor.__new__(Executor)
    ex.broker = broker

    async def quotes(contracts):
        return {q.contract.key: q for q in await broker.quotes(contracts)}

    ex.quotes = quotes
    n = Notes()
    asyncio.run(
        run._external_calls(ex, j, TODAY, n)
    )  # pylint: disable=protected-access
    rec = j.latest("job_runs", key="external_calls").data["recommendations"][0]
    assert rec["contracts"] == 6 and abs(rec["delta"] - 0.15) < 0.05
    assert n.sent[0][0] == "report" and "Sell 6 META" in n.sent[0][3]
    # Once a day: a second scan doesn't recommend again.
    asyncio.run(
        run._external_calls(ex, j, TODAY, n)
    )  # pylint: disable=protected-access
    assert len(j.read("job_runs", key="external_calls")) == 1
    # A recorded call that's now too close to the money -> a roll alert.
    j.append("external_trades", sale(610, exp=EXP), key="x")
    asyncio.run(
        run._external_calls(ex, j, TODAY, n)
    )  # pylint: disable=protected-access
    assert any(s[1].startswith("ext-roll:") for s in n.sent)
    set_clock(None)


def test_pages(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    (tmp_path / "ext.json").write_text(
        json.dumps({"META": {"shares": 1000, "cc_calls": 6}})
    )
    monkeypatch.setenv("TRADING_EXTERNAL_HOLDINGS", str(tmp_path / "ext.json"))
    c = create_app().test_client()
    assert "Record a call you sold" in c.get("/bot/external").get_data(as_text=True)
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    exp = (date.today() + timedelta(days=30)).isoformat()
    c.post(
        "/bot/external",
        data={
            "csrf_token": "t",
            "action": "sell",
            "symbol": "meta",
            "expiry": exp,
            "strike": "685",
            "contracts": "6",
            "price": "5.40",
        },
    )
    recs = [r.data for r in Journal(tmp_path / "j.db").read("external_trades")]
    assert recs[0]["symbol"] == "META" and recs[0]["contracts"] == 6
    html = c.get("/bot/external").get_data(as_text=True)
    assert "META " in html and "685" in html
    bad = c.post(
        "/bot/external",
        data={
            "csrf_token": "t",
            "action": "sell",
            "symbol": "META",
            "expiry": exp,
            "strike": "685",
            "contracts": "-1",
            "price": "1",
        },
    )
    assert (
        bad.status_code == 302
        and len(Journal(tmp_path / "j.db").read("external_trades")) == 1
    )
    monkeypatch.setattr(
        "src.web.app.load_trading_config",
        lambda *a, **k: TradingConfig(income_goal_monthly=5000),
    )
    html = c.get("/bot/metrics").get_data(as_text=True)
    assert "of $5,000 goal" in html and "$3,240" in html  # 6 x 100 x $5.40 this month
