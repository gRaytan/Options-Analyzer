"""Crash check and rebuild preview from an uploaded IB statement (no IB needed)."""
from datetime import date, datetime

import pytest

from src.models.portfolio import Portfolio
from src.models.position import OptionPosition, OptionType, StockPosition, Trade
from src.trading import rebuild
from src.trading import statement as st
from src.trading.config import load_trading_config
from src.trading.domain import OptionContract, StockContract

CFG = load_trading_config("data/config/trading.json")
END = date(2026, 11, 20)


def portfolio(maint=400_000.0, el=350_000.0):
    p = Portfolio()
    p.account_id = "U1234567"
    p.period_end = END
    p.statement_period = "November 2026"
    p.net_liquidation_value = 900_000.0
    p.excess_liquidity = el
    p.maintenance_margin = maint
    p.initial_margin = maint * 1.2
    p.cash_balance = -20_000.0
    p.stocks = [
        StockPosition("SPY", 500, 600.0, 670.0),
        StockPosition("QQQM", 1200, 220.0, 250.0),
        StockPosition("NBIS", 300, 80.0, 100.0),
    ]
    p.options = [
        OptionPosition(
            "XSP 18DEC26 600 P",
            -5,
            3.0,
            2.0,
            underlying="XSP",
            strike=600.0,
            expiration=date(2026, 12, 18),
            option_type=OptionType.PUT,
            underlying_price=690.0,
            delta=-0.12,
            implied_volatility=24.0,
        ),
    ]
    p.trades = [
        Trade(
            "BRK B",
            datetime(2026, 11, 3, 15, 0),
            -200,
            482.5,
            96_500.0,
            -1.0,
            0.0,
            asset_type="Stock",
        ),
    ]
    return p


def test_from_portfolio():
    book = st.from_portfolio(portfolio())
    assert book.as_of == END
    a = book.account
    assert (a.net_liquidation, a.excess_liquidity, a.maint_margin, a.cash) == (
        900_000,
        350_000,
        400_000,
        -20_000,
    )
    stocks = {
        p.contract.symbol: p
        for p in book.positions
        if isinstance(p.contract, StockContract)
    }
    assert stocks["SPY"].market_price == 670.0 and stocks["NBIS"].quantity == 300
    [opt] = [p for p in book.positions if isinstance(p.contract, OptionContract)]
    assert opt.contract.cash_settled and opt.quantity == -5
    q = book.quotes[opt.contract.key]
    assert (
        q.mid == 2.0
        and q.greeks.iv == pytest.approx(0.24)
        and q.underlying_price == 690.0
    )
    # BRK B was sold: its price comes from the last trade.
    assert book.prices()["BRK B"] == 482.5 and book.prices()["SPY"] == 670.0


def test_missing_margin_is_estimated():
    book = st.from_portfolio(portfolio(maint=0.0, el=0.0))
    assert book.account.maint_margin > 0
    assert book.account.excess_liquidity == pytest.approx(
        900_000 - book.account.maint_margin
    )
    assert any("estimated" in n for n in book.notes)


def test_crash_table_and_margin_call():
    rows = st.crash_table(st.from_portfolio(portfolio()), CFG)
    assert [r.move for r in rows] == [-0.10, -0.20, -0.30]
    # Stocks: SPY $335k x1, QQQM $300k x1.2, NBIS $30k x2.5 -> -30%: 100.5k + 108k + 22.5k.
    assert rows[-1].stock_pnl == pytest.approx(
        -(335_000 * 0.30 + 300_000 * 0.36 + 30_000 * 0.75)
    )
    assert rows[-1].options_pnl < 0  # the short XSP put loses
    assert rows[0].nlv_after > rows[1].nlv_after > rows[2].nlv_after
    assert not rows[0].margin_call  # -10%: EL stays above $100k
    tight = st.crash_table(st.from_portfolio(portfolio(maint=780_000, el=120_000)), CFG)
    assert tight[-1].margin_call


def test_loss_by_position_worst_first():
    worst = st.by_position(st.from_portfolio(portfolio()), CFG, -0.30)
    names = [w[0] for w in worst]
    assert names[0] == "QQQM" and "XSP options" in names
    assert sum(w[2] for w in worst) < 0


def test_no_room_when_cushion_is_under_40():
    book = st.from_portfolio(portfolio())  # Cushion 39%
    steps, fit, notes = rebuild.plan_shares(
        book.positions,
        book.quotes,
        book.account,
        book.prices(),
        {"SPY": 0.5},
        CFG,
        "elevated",
        END,
    )
    assert steps == [] and "Restore the margin cushion" in notes[-1]


def test_margin_multiplier_scales_with_the_drop():
    assert st.margin_multiplier(-0.30, CFG) == pytest.approx(1.5)
    assert st.margin_multiplier(-0.10, CFG) == pytest.approx(1 + 0.5 / 3)
    assert st.margin_multiplier(-0.50, CFG) == pytest.approx(1.5)


def test_rebuild_preview_uses_statement_prices():
    book = st.from_portfolio(portfolio(maint=200_000, el=650_000))
    steps, fit, notes = rebuild.plan_shares(
        book.positions,
        book.quotes,
        book.account,
        book.prices(),
        rebuild.parse_targets("SPY=40%, QQQM=40%, BRK.B=12%"),
        CFG,
        "elevated",
        END,
    )
    by = {s.symbol: s for s in steps}
    assert "BRK B" in by and by["BRK B"].price == 482.5
    assert fit.stress_el_after >= 100_000


def client(tmp_path, monkeypatch, with_portfolio=True):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    app = create_app()
    if with_portfolio:
        app.portfolios = {"main": portfolio()}
    return app.test_client()


def test_page_without_a_statement(tmp_path, monkeypatch):
    html = client(tmp_path, monkeypatch, False).get("/bot/check").get_data(as_text=True)
    assert "Upload an IB Activity Statement" in html


def test_page_with_a_statement(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    html = c.get("/bot/check").get_data(as_text=True)
    assert (
        "If the S&amp;P 500 falls" in html and "Margin call?" in html and "QQQM" in html
    )
    html = c.get(
        "/bot/check?targets=SPY%3D40%25%2C+BRK.B%3D12%25&regime=elevated"
    ).get_data(as_text=True)
    assert "Rebuild preview" in html and "BRK B" in html and "Preview only" in html
