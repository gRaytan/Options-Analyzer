"""Risk analyzer flags added for the trading bot (plan T24, spec §5.7)."""
from datetime import date, timedelta

import pytest

from src.analysis.risk import HIGH_RISK_FLAGS, RiskAnalyzer, analyze_portfolio_risk
from src.models.portfolio import Portfolio
from src.models.position import OptionPosition, OptionType
from src.trading.calendars import Dividend, EventCalendar, SymbolEvents

TODAY = date.today()


def opt(
    kind=OptionType.PUT,
    strike=100,
    und=110,
    qty=-1,
    cost=1.0,
    price=0.5,
    delta=-0.2,
    dte=30,
    underlying="AAPL",
):
    return OptionPosition(
        symbol=f"{underlying} X",
        underlying=underlying,
        strike=strike,
        expiration=TODAY + timedelta(days=dte),
        option_type=kind,
        quantity=qty,
        cost_price=cost,
        current_price=price,
        underlying_price=und,
        delta=delta,
    )


def flags(o, **kw):
    return {f.flag_type for f in RiskAnalyzer(**kw).analyze_position(o).flags}


def test_high_delta_short_put():
    assert "HIGH_DELTA_SHORT_PUT" in flags(opt(delta=-0.40))
    assert "HIGH_DELTA_SHORT_PUT" not in flags(opt(delta=-0.30))
    assert "HIGH_DELTA_SHORT_PUT" in flags(
        opt(delta=-0.30), config={"high_delta_put_threshold": 0.25}
    )


def test_large_loss_relative_to_credit():
    assert "LARGE_LOSS" in flags(opt(cost=1.0, price=2.0))  # 2x the premium
    assert "LARGE_LOSS" not in flags(opt(cost=1.0, price=1.9))
    # A big dollar loss on a big position is no longer the trigger by itself:
    big = opt(cost=10.0, price=15.0, qty=-5)  # -$2,500 but only 1.5x
    assert "LARGE_LOSS" not in flags(big)


def test_pin_risk():
    assert "PIN_RISK" in flags(opt(strike=100, und=101.5, dte=3))
    assert "PIN_RISK" not in flags(opt(strike=100, und=103, dte=3))
    assert "PIN_RISK" not in flags(opt(strike=100, und=101, dte=10))


class Events:
    def __init__(self, earnings=None, dividend=None):
        self._earnings, self._div = earnings, dividend

    def earnings_before(self, symbol, expiry, today):
        return self._earnings

    def ex_dividend_before(self, symbol, expiry, today):
        return self._div


def test_earnings_flag_needs_calendar():
    o = opt()
    assert "EARNINGS_BEFORE_EXPIRY" not in flags(o)  # no calendar: skipped
    assert "EARNINGS_BEFORE_EXPIRY" in flags(o, events=Events(earnings=True))
    assert "EARNINGS_BEFORE_EXPIRY" not in flags(o, events=Events(earnings=None))


def test_ex_dividend_assignment_risk():
    div = Dividend(TODAY + timedelta(days=10), 0.30)
    itm_call = opt(
        OptionType.CALL, strike=100, und=110, cost=2.0, price=10.1, delta=0.9
    )  # extrinsic 0.10
    assert "EX_DIVIDEND_ASSIGNMENT_RISK" in flags(itm_call, events=Events(dividend=div))
    rich = opt(
        OptionType.CALL, strike=100, und=110, cost=2.0, price=11.0, delta=0.9
    )  # extrinsic 1.00
    assert "EX_DIVIDEND_ASSIGNMENT_RISK" not in flags(rich, events=Events(dividend=div))
    otm = opt(OptionType.CALL, strike=120, und=110, price=0.5, delta=0.2)
    assert "EX_DIVIDEND_ASSIGNMENT_RISK" not in flags(otm, events=Events(dividend=div))


def test_summary_counts_and_high_risk_set():
    p = Portfolio()
    p.options = [opt(delta=-0.5), opt(strike=100, und=101, dte=2), opt(cost=1, price=3)]
    s = analyze_portfolio_risk(p).summary
    assert s["high_delta_puts"] >= 1 and s["pin_risk"] == 1 and s["large_losses"] == 1
    assert {"PIN_RISK", "HIGH_DELTA_SHORT_PUT"} <= HIGH_RISK_FLAGS


def test_works_with_real_event_calendar(tmp_path):
    over = tmp_path / "o.json"
    over.write_text(
        '{"AAPL": {"earnings": "%s"}}' % (TODAY + timedelta(days=5)).isoformat()
    )
    cal = EventCalendar(None, overrides_path=over, cache_path=tmp_path / "c.json")
    assert "EARNINGS_BEFORE_EXPIRY" in flags(opt(), events=cal)


def test_pages_still_render_with_new_flags(tmp_path, monkeypatch):
    from src.web.app import create_app

    app = create_app()
    p = Portfolio()
    p.options = [opt(delta=-0.5), opt(strike=100, und=101, dte=2)]
    app.portfolios = {"t": p}
    app.risk_analyses = {"t": analyze_portfolio_risk(p)}
    c = app.test_client()
    for page in ("/positions?risk=high", "/recommendations"):
        resp = c.get(page)
        assert resp.status_code == 200, page
