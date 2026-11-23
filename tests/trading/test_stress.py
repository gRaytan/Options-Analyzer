"""Stress test and risk budget (plan T19, spec §6.2)."""
from datetime import date, datetime, timezone

import pytest
from py_vollib.black_scholes import black_scholes

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import (
    Scenario,
    StressModel,
    standard_scenarios,
    stress_checks,
)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)
DEC = date(2026, 12, 31)  # 38 days
CFG = TradingConfig()

XSP = OptionContract(
    "XSP", DEC, 600, Right.PUT, style=ExerciseStyle.EUROPEAN, cash_settled=True
)
SPY_HEDGE = OptionContract("SPY", date(2027, 2, 19), 570, Right.PUT)
AAPL_PUT = OptionContract("AAPL", DEC, 200, Right.PUT)
QQQM, SPY, AAPL = StockContract("QQQM"), StockContract("SPY"), StockContract("AAPL")

QUOTES = {
    XSP.key: Quote(
        XSP, 0.6, 0.7, NOW, underlying_price=660, greeks=Greeks(-0.05, iv=0.18)
    ),
    SPY_HEDGE.key: Quote(
        SPY_HEDGE, 3.0, 3.2, NOW, underlying_price=670, greeks=Greeks(-0.08, iv=0.22)
    ),
    AAPL_PUT.key: Quote(
        AAPL_PUT, 1.5, 1.6, NOW, underlying_price=230, greeks=Greeks(-0.12, iv=0.30)
    ),
}
HOLDINGS = [Position(QQQM, 600, 180.0, 250.0), Position(SPY, 400, 520.0, 670.0)]
ACCOUNT = AccountSnapshot("DU1", NOW, 1_000_000, 700_000, 300_000, 300_000, 650_000)


def checks(positions, intent=None, account=ACCOUNT, regime=1.0, betas=None):
    exp = exposures_from(positions, QUOTES)
    d = stress_checks(intent, exp, QUOTES, account, CFG, TODAY, regime, betas)
    return {c.name: c for c in d.checks}


def test_fully_invested_without_options_passes():
    """Regression for the v0.3 spec bug: stocks alone must not fail the stress test."""
    res = checks(HOLDINGS)
    assert all(c.passed for c in res.values()), res


def test_stock_and_margin_math_by_hand():
    model = StressModel(exposures_from(HOLDINGS, QUOTES), QUOTES, ACCOUNT, TODAY)
    r = model.run(Scenario("x", -0.25, 0.5, 1.5))
    assert r.stock_pnl == pytest.approx(-0.25 * (150_000 + 268_000))
    assert r.options_pnl == 0
    # Model margin is 25% of stock both before and after -> ratio 0.75; IB $300k x 0.75 x 1.5.
    assert r.maint_margin_after == pytest.approx(300_000 * 0.75 * 1.5)
    assert r.excess_liquidity_after == pytest.approx(1_000_000 - 104_500 - 337_500)


def per_contract_loss():
    base = black_scholes("p", 660, 600, 38 / 365, 0.04, 0.18)
    shocked = black_scholes("p", 528, 600, 38 / 365, 0.04, 0.27)
    return (shocked - base) * 100  # about $7,058


def test_budget_scenario_by_hand():
    loss = per_contract_loss()
    assert 6_500 < loss < 7_500
    ok = checks(HOLDINGS + [Position(XSP, -14, 0.65)])["stress_budget"]
    assert ok.passed  # 14 x ~7.06k = ~98.8k <= $100k budget
    bad = checks(HOLDINGS + [Position(XSP, -15, 0.65)])["stress_budget"]
    assert not bad.passed and "budget $100,000" in bad.detail


def test_trade_is_tested_on_the_portfolio_after_it():
    held = HOLDINGS + [Position(XSP, -14, 0.65)]
    one_more = TradeIntent(
        "i", "s", IntentKind.OPEN, (OrderLeg(XSP, Action.SELL, 1),), "T", NOW
    )
    assert not checks(held, one_more)["stress_budget"].passed


def test_regime_multiplier_shrinks_budget():
    held = HOLDINGS + [Position(XSP, -10, 0.65)]  # ~$70.6k loss
    assert checks(held)["stress_budget"].passed
    assert not checks(held, regime=0.5)["stress_budget"].passed  # budget $50k


def test_hedge_offsets_options_loss():
    held = HOLDINGS + [Position(XSP, -15, 0.65)]
    assert not checks(held)["stress_budget"].passed
    hedged = held + [Position(SPY_HEDGE, 10, 3.1)]
    assert checks(hedged)["stress_budget"].passed


def test_stress_el_floor_on_a_small_leveraged_account():
    small = AccountSnapshot("DU1", NOW, 300_000, 120_000, 180_000, 180_000, 100_000)
    held = [Position(SPY, 1000, 520.0, 670.0), Position(XSP, -3, 0.65)]
    res = checks(held, account=small)
    assert not res["stress_el"].passed and "floor $100,000" in res["stress_el"].detail


def test_single_name_gap():
    # AAPL -35% to 149.5 with IV +50%: each 200 put goes ~0.63 -> ~49.9, about $4,927.
    # Limit is 2% NLV = $20k: 4 contracts (~$19.7k) pass, 5 (~$24.6k) fail.
    assert checks(HOLDINGS + [Position(AAPL_PUT, -4, 1.55)])["single_name_gap"].passed
    gap = checks(HOLDINGS + [Position(AAPL_PUT, -5, 1.55)])["single_name_gap"]
    assert not gap.passed and "AAPL" in gap.detail


def test_index_puts_are_not_single_name_gaps():
    assert checks(HOLDINGS + [Position(XSP, -14, 0.65)])["single_name_gap"].passed


def test_beta_floor_on_downside():
    exp = exposures_from(HOLDINGS, QUOTES)
    low = StressModel(exp, QUOTES, ACCOUNT, TODAY, betas={"QQQM": 0.5})
    high = StressModel(exp, QUOTES, ACCOUNT, TODAY, betas={"QQQM": 1.3})
    down = Scenario("d", -0.20)
    base = StressModel(exp, QUOTES, ACCOUNT, TODAY).run(down).stock_pnl
    assert low.run(down).stock_pnl == pytest.approx(base)  # beta 0.5 floored to 1.0
    assert high.run(down).stock_pnl < base  # beta 1.3 falls further
    assert low.run(Scenario("u", 0.20)).stock_pnl < base * -1  # upside keeps beta 0.5


def test_risk_reducing_skips():
    close = TradeIntent(
        "c",
        "s",
        IntentKind.CLOSE,
        (OrderLeg(XSP, Action.BUY, 1),),
        "T",
        NOW,
        risk_reducing=True,
    )
    res = checks(HOLDINGS + [Position(XSP, -50, 0.65)], close)
    assert list(res) == ["stress"] and res["stress"].passed


def test_standard_scenarios():
    names = [s.name for s in standard_scenarios(CFG)]
    # The -30% crash (risk.crash_move) is in the grid too.
    assert names[:8] == ["-30%", "-25%", "-20%", "-15%", "-8%", "+8%", "+15%", "+25%"]
    assert {"-15% IV+50%", "-20% IV+50%", "-25% IV+50%", "-30% IV+50%"} <= set(names)
    assert all(
        s.margin_multiplier == 1.5 for s in standard_scenarios(CFG) if s.market_move < 0
    )
