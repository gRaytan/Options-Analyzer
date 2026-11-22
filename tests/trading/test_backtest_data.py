"""Synthetic backtest data (plan T36, spec §11.1)."""
import asyncio
import math
from datetime import date, timedelta

import pytest
from py_vollib.black_scholes_merton.implied_volatility import implied_volatility

from src.trading.backtest import data
from src.trading.backtest.synthetic import (
    SyntheticMarket,
    compare_quotes,
    expiries,
    strikes,
)
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import AccountSnapshot, Bar, HistoryKind, Right
from src.trading.history import HistoryStore
from src.trading.scanner import scan

DAY = date(2026, 11, 20)  # a Friday

CBOE = """DATE,OPEN,HIGH,LOW,CLOSE
11/19/2026,30.0,31.0,29.0,30.50
11/20/2026,31.36,46.21,27.35,40.72
bad,row,,,
"""


def market(vix=20.0, vix9d=None, vix3m=None, vxn=None, price=600.0, day=DAY):
    h = data.MarketHistory()
    h.bars["SPY"] = [Bar(day, price, price, price, price)]
    h.bars["QQQ"] = [Bar(day, 500, 500, 500, 500)]
    h.bars["AAPL"] = [Bar(day, 200, 200, 200, 200)]
    h.vol["VIX"] = {day: vix}
    for name, v in (("VIX9D", vix9d), ("VIX3M", vix3m), ("VXN", vxn)):
        if v is not None:
            h.vol[name] = {day: v}
    return SyntheticMarket(h)


# -- data ------------------------------------------------------------------------------


def test_parse_cboe_csv_skips_bad_rows():
    assert data.parse_cboe_csv(CBOE) == {date(2026, 11, 19): 30.5, DAY: 40.72}


def test_bars_csv_round_trip_and_yfinance_headers():
    bars = [Bar(DAY, 1.0, 2.0, 0.5, 1.5, 100.0)]
    assert data.parse_bars_csv(data.bars_to_csv(bars)) == bars
    yahoo = (
        "Date,Open,High,Low,Close,Adj Close,Volume\n2026-11-20,1,2,0.5,1.5,1.4,100\n"
    )
    assert data.parse_bars_csv(yahoo) == bars


def test_fetch_cboe_caches_good_answers_only(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        return CBOE if "VIX_" in url else "DATE,OPEN,HIGH,LOW,CLOSE\n"

    assert data.fetch_cboe("VIX", tmp_path, get=get)[DAY] == 40.72
    assert data.fetch_cboe("VIX", tmp_path, get=get)[DAY] == 40.72
    assert len(calls) == 1 and "VIX_History.csv" in calls[0]
    assert data.fetch_cboe("VXN", tmp_path, get=get) == {}
    assert not (tmp_path / "VXN.cboe.csv").exists()


def test_load_history_adds_xsp_from_spx_or_spy(tmp_path):
    data.save_bars("SPX", [Bar(DAY, 6000, 6010, 5990, 6005)], tmp_path)
    h = data.load_history(["SPX"], tmp_path, get=lambda url: CBOE)
    assert h.bars["XSP"][0].close == pytest.approx(600.5)
    assert h.vol_on("VIX", DAY) == pytest.approx(0.4072)
    h2 = data.MarketHistory(bars={"SPY": [Bar(DAY, 1, 1, 1, 598.0)]}).with_xsp()
    assert h2.bars["XSP"][0].close == 598.0


def test_recorded_chain_csv_round_trip():
    m = market()
    c = m.contract("XSP", DAY + timedelta(days=35), 560, Right.PUT)
    m.history.bars["XSP"] = m.history.bars["SPY"]
    m._closes["XSP"] = m.history.closes("XSP")  # pylint: disable=protected-access
    q = m.quote(c, DAY)
    day, und, [back] = data.recorded_from_csv(data.recorded_to_csv([q], DAY))
    assert (day, und) == (DAY, 600.0)
    assert back.contract == c and back.bid == q.bid and back.ask == q.ask
    assert back.greeks.iv == pytest.approx(q.greeks.iv)


# -- implied vol ---------------------------------------------------------------------------


def test_term_structure_interpolates_in_variance():
    m = market(vix=20, vix9d=30, vix3m=18)
    r = m.p.atm_ratio
    assert m.atm_iv("SPY", DAY, 5) == pytest.approx(0.30 * r)
    assert m.atm_iv("SPY", DAY, 30) == pytest.approx(0.20 * r)
    assert m.atm_iv("SPY", DAY, 200) == pytest.approx(0.18 * r)
    w = 0.2**2 * 30 + (0.18**2 * 93 - 0.2**2 * 30) * (60 - 30) / 63
    assert m.atm_iv("SPY", DAY, 60) == pytest.approx(math.sqrt(w / 60) * r)


def test_qqq_uses_vxn_and_stocks_have_no_iv():
    m = market(vix=20, vix9d=30, vix3m=18, vxn=25)
    # VXN shaped by the SPX term structure: 25 x (30 / 20) at 9 days.
    assert m.atm_iv("QQQ", DAY, 9) == pytest.approx(0.25 * 1.5 * m.p.atm_ratio)
    assert m.atm_iv("AAPL", DAY, 30) is None
    m2 = market(vix=20)
    assert m2.atm_iv("QQQ", DAY, 30) == pytest.approx(0.25 * m2.p.atm_ratio)
    assert any("VXN" in f for f in m2.fallbacks)


def test_skew_is_conservative_and_steeper_when_vix_is_low():
    m = market(vix=20)
    t, atm = 35 / 365, 0.18
    z10 = -1.3  # about a 10-delta put
    k10 = 600 * math.exp(z10 * atm * math.sqrt(t))
    pts = (m.skewed_iv(k10, 600, atm, t, 0.20) - atm) * 100
    assert 1.5 < pts < 5  # real SPX skew is ~6-8 points: we understate premium
    low = m.skewed_iv(k10, 600, atm, t, 0.12) - atm
    high = m.skewed_iv(k10, 600, atm, t, 0.40) - atm
    assert low > high > 0
    assert m.skewed_iv(660, 600, atm, t, 0.20) < atm  # OTM calls a bit below ATM


# -- quotes and chains ---------------------------------------------------------------------


def test_quote_prices_greeks_and_pessimistic_spread():
    m = market(vix=20)
    exp = DAY + timedelta(days=35)
    put = m.quote(m.contract("SPY", exp, 570, Right.PUT), DAY)
    call = m.quote(m.contract("SPY", exp, 570, Right.CALL), DAY)
    assert put.greeks.delta < 0 < call.greeks.delta
    assert put.ask - put.bid >= max(0.05, 0.05 * put.mid) - 0.011
    cheap = m.quote(m.contract("SPY", exp, 450, Right.PUT), DAY)
    assert cheap.ask - cheap.bid >= 0.04  # $0.05 minimum width (rounded)
    # The quoted IV is the one actually used in the price.
    t = 35 / 365
    iv = implied_volatility(
        put.mid, 600, 570, t, m.p.rate, m.p.dividend_yield["SPY"], "p"
    )
    assert iv == pytest.approx(put.greeks.iv, abs=0.01)
    assert (
        m.quote(m.contract("SPY", exp, 570, Right.PUT), DAY + timedelta(days=1)) is None
    )


def test_expiries_weekly_monthly_and_holidays():
    ex = expiries(DAY)
    assert ex[0] == date(2026, 11, 27) and date(2026, 12, 24) in ex  # Christmas Friday
    assert date(2026, 12, 25) not in ex and date(2027, 3, 19) in ex
    assert date(2020, 4, 9) in expiries(date(2020, 4, 1))  # Good Friday -> Thursday
    assert all(e > DAY for e in ex) and len(ex) == len(set(ex))


def test_strikes_fine_near_the_money():
    ks = strikes(600, "SPY")
    assert 599 in ks and 601 in ks and 600 in ks
    assert 450 in ks and 451 not in ks  # $5 beyond 15%
    assert min(ks) >= 360 and max(ks) <= 840
    assert all(k % 5 == 0 for k in strikes(6000, "SPX"))


def test_compare_quotes():
    m = market(vix=20)
    exp = DAY + timedelta(days=35)
    qs = [m.quote(m.contract("SPY", exp, k, Right.PUT), DAY) for k in (540, 560, 580)]
    same = compare_quotes(qs, {q.contract.key: q for q in qs})
    assert same.n == 3 and same.mean_abs_error == 0 and same.within()
    pricier = market(vix=30)
    synth = {q.contract.key: q for q in pricier.quotes([q.contract for q in qs], DAY)}
    cmp_ = compare_quotes(qs, synth)
    assert cmp_.mean_iv_error > 0 and cmp_.worst[1] > 0


# -- into the bot: scanner on a synthetic day ------------------------------------------------


def test_scanner_finds_index_puts_on_a_synthetic_day(tmp_path):
    import random

    rnd = random.Random(3)
    h = data.MarketHistory()
    days, price, vix = [], 5800.0, {}
    d = DAY - timedelta(days=420)
    bars = []
    while d <= DAY:
        if d.weekday() < 5:
            price *= math.exp(rnd.gauss(0, 0.008))
            bars.append(Bar(d, price, price * 1.004, price * 0.996, price))
            vix[d] = 14 + 10 * (len(bars) % 60) / 59  # 14..24
            days.append(d)
        d += timedelta(days=1)
    vix[days[-1]] = 23.0
    h.bars["SPX"] = bars
    h.vol["VIX"] = vix
    m = SyntheticMarket(h)
    today = days[-1]
    m.day = today

    broker = FakeAdapter(account_id="DU1", read_only=True)
    asyncio.run(broker.connect())
    broker.set_history("XSP", HistoryKind.PRICE, h.bars["XSP"])
    broker.set_history("XSP", HistoryKind.IMPLIED_VOL, m.iv_series("XSP", today))
    broker.set_chain("XSP", m.chain("XSP", today))
    broker.set_quote_source(m.quote)
    cfg = TradingConfig.model_validate(
        {"enabled_strategies": {"index_put": {"manage": True, "open": True}}}
    )
    acct = AccountSnapshot("DU1", None, 1_000_000, 700_000, 300_000, 300_000, 650_000)
    result = asyncio.run(
        scan(broker, HistoryStore(broker, tmp_path), [], acct, cfg, today)
    )
    assert result.candidates, result.rejected
    best = result.best(1)[0]
    assert (
        best.contract.underlying == "XSP"
        and 0.10 <= abs(best.quote.greeks.delta) <= 0.16
    )
