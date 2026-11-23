"""Bot metrics from the journal (plan T40)."""
import csv
import io
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import metrics
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Fill,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Right,
    TradeIntent,
)
from src.trading.journal import Journal

T0 = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
DEC = date(2026, 12, 18)
NOV = date(2026, 11, 20)


def xsp(k, exp=DEC):
    return OptionContract(
        "XSP", exp, k, Right.PUT, 100, ExerciseStyle.EUROPEAN, True, "XSP"
    )


def intent(ref, strategy, kind, legs, trigger="SCAN_READY"):
    return TradeIntent(ref, strategy, kind, tuple(legs), trigger, T0)


class Book:
    """A journal with orders, intents and fills written the way the engine does."""

    def __init__(self, path):
        self.j = Journal(path)
        self.n = 0

    def trade(self, ref, strategy, kind, legs, mid, fills, at, trigger="SCAN_READY"):
        """legs: [(contract, action, qty)]; fills: per-leg prices."""
        it = intent(
            ref, strategy, kind, [OrderLeg(c, a, q) for c, a, q in legs], trigger
        )
        self.j.append("intents", it, key=ref)
        self.j.append("orders", OrderRequest(ref, it.legs, mid, ref), key=ref)
        for (c, a, q), price in zip(legs, fills):
            self.n += 1
            f = Fill(f"e{self.n}", ref, c, a, q, price, at, 0.65 * q + 0.10 * q)
            self.j.append("fills", f, key=ref)


@pytest.fixture
def book(tmp_path):
    b = Book(tmp_path / "j.db")
    # 1. Index put: sold 5 at 3.00, bought back at 1.00 -> win.
    b.trade(
        "o1",
        "index_put",
        IntentKind.OPEN,
        [(xsp(600), Action.SELL, 5)],
        -3.05,
        [3.00],
        T0,
    )
    b.trade(
        "c1",
        "position_manager",
        IntentKind.CLOSE,
        [(xsp(600), Action.BUY, 5)],
        1.0,
        [1.00],
        T0 + timedelta(days=20),
        "REPLACEMENT",
    )
    # 2. Put spread (one combo), still open.
    b.trade(
        "o2",
        "put_spread",
        IntentKind.OPEN,
        [(xsp(590), Action.SELL, 4), (xsp(585), Action.BUY, 4)],
        -0.80,
        [2.60, 1.85],
        T0,
    )
    # 3. Index put rolled once, then expired worthless after the roll.
    b.trade(
        "o3",
        "index_put",
        IntentKind.OPEN,
        [(xsp(610, NOV), Action.SELL, 2)],
        -2.0,
        [2.00],
        T0,
    )
    b.trade(
        "r3",
        "assignment_guard",
        IntentKind.ROLL,
        [
            (xsp(610, NOV), Action.BUY, 2),
            (xsp(600, NOV + timedelta(days=7)), Action.SELL, 2),
        ],
        -0.30,
        [4.00, 4.20],
        T0 + timedelta(days=10),
        "EARLY_ROLL",
    )
    return b


def test_trades_group_spreads_and_rolls(book):
    m = metrics.compute(book.j, today=date(2026, 12, 1))
    by = {t.strategy: t for t in m.trades}
    assert len(m.trades) == 3
    rolled = [t for t in m.trades if t.rolls == 1][0]
    assert rolled.strategy == "index_put" and len(rolled.keys) == 2
    assert rolled.is_closed and rolled.expired_worthless == ["XSP 2026-11-27 P 600"]
    # premium 2x2x100 + roll (4.20-4.00)x2x100 = 440, minus fees 3 fills x 2 contracts x $0.75
    assert rolled.pnl == pytest.approx(400 - 800 + 840 - 3 * 2 * 0.75)
    spread = by["put_spread"]
    assert not spread.is_closed and len(spread.keys) == 2
    simple = [t for t in m.trades if t.keys == ["XSP 2026-12-18 P 600"]][0]
    assert simple.pnl == pytest.approx(
        1500 - 500 - 2 * 5 * 0.75
    ) and simple.closed == date(2026, 10, 21)
    assert m.win_rate == 1.0 and len(m.closed) == 2
    assert (
        m.strategies["index_put"].closed == 2 and m.strategies["put_spread"].closed == 0
    )
    assert m.roll_rate == pytest.approx(1 / 3)


def test_fees_and_slippage(book):
    m = metrics.compute(book.j, today=date(2026, 12, 1))
    assert m.premium_sold == pytest.approx(1500 + 1040 + 400 + 840)
    assert m.fees_pct == pytest.approx(m.fees / m.premium_sold)
    slip = {s["order"]: s for s in m.slippage}
    # Sold at 3.00 vs a 3.05 mid: gave up 0.05 x 5 x 100 = $25.
    assert slip["o1"]["slippage"] == pytest.approx(25)
    # Spread: net credit 0.75 vs 0.80 mid: $20 on 4 contracts.
    assert slip["o2"]["slippage"] == pytest.approx(20)
    assert slip["c1"]["slippage"] == pytest.approx(0)


def test_monthly_and_realized_csv(book):
    m = metrics.compute(book.j, today=date(2026, 12, 1))
    assert m.months["2026-10"]["premium"] == pytest.approx(m.premium_sold)
    assert m.months["2026-10"]["pnl"] == pytest.approx(
        [t for t in m.closed if t.closed.month == 10][0].pnl
    )
    rows = list(csv.DictReader(io.StringIO(metrics.realized_csv(m.trades, 2026))))
    assert len(rows) == 2 and {r["strategy"] for r in rows} == {"index_put"}
    assert any("expired worthless" in r["note"] for r in rows)
    assert metrics.realized_csv(m.trades, 2025).count("\n") == 1  # header only


def test_nlv_drawdown_and_sortino(tmp_path):
    j = Journal(tmp_path / "j.db")
    vals = [1_000_000 * (1 + 0.001 * ((i % 7) - 3)) for i in range(40)]
    for i, v in enumerate(vals):
        ts = datetime(2026, 10, 1, 20, tzinfo=timezone.utc) + timedelta(days=i)
        j.append(
            "account_snapshot",
            AccountSnapshot("DU1", ts, v, 0.7 * v, 0.3 * v, 0.3 * v, 0.6 * v),
            ts=ts,
        )
    m = metrics.compute(j, today=date(2026, 11, 15))
    assert len(m.nlv) == 40 and m.max_drawdown > 0 and m.sortino is not None
    assert metrics.drawdown([100, 80, 120]) == pytest.approx(0.2)


# -- the page -------------------------------------------------------------------------


def client(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    return create_app().test_client()


def test_page_empty(tmp_path, monkeypatch):
    html = client(tmp_path, monkeypatch).get("/bot/metrics").get_data(as_text=True)
    assert "No bot trades yet" in html


def test_page_renders_from_the_journal(book, tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    html = c.get("/bot/metrics").get_data(as_text=True)
    assert "Win rate" in html and "put spread" in html and "index put" in html
    assert "monthlyChart" in html and "2026 CSV" in html
    resp = c.get("/bot/metrics/realized.csv?year=2026")
    assert resp.mimetype == "text/csv" and "realized_pnl_usd" in resp.get_data(
        as_text=True
    )
    assert "attachment" in resp.headers["Content-Disposition"]
