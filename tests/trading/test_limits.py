"""Hard risk limits (plan T18a, spec §6.1)."""
from datetime import date, datetime, timezone

import pytest

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    MarginImpact,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.risk.limits import (
    Exposure,
    LimitContext,
    exposures_from,
    gate_decision,
)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
NLV = 1_000_000
DEC = date(2026, 12, 31)
JAN = date(2027, 1, 29)


def xsp_put(strike=600, expiry=DEC):
    return OptionContract(
        "XSP",
        expiry,
        strike,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
    )


QQQM = StockContract("QQQM")
SPY = StockContract("SPY")
SPY_CALL = OptionContract("SPY", DEC, 720, Right.CALL)
SPY_PUT = OptionContract("SPY", DEC, 620, Right.PUT)
QQQM_PUT = OptionContract("QQQM", DEC, 230, Right.PUT)
SPXW_PUT = OptionContract("SPX", DEC, 6000, Right.PUT, trading_class="SPXW")


def quote(contract, delta, und):
    return Quote(contract, 1.0, 1.1, NOW, underlying_price=und, greeks=Greeks(delta))


QUOTES = {
    xsp_put().key: quote(xsp_put(), -0.14, 660),
    xsp_put(expiry=JAN).key: quote(xsp_put(expiry=JAN), -0.15, 660),
    SPY_CALL.key: quote(SPY_CALL, 0.12, 670),
    SPY_PUT.key: quote(SPY_PUT, -0.15, 670),
    QQQM_PUT.key: quote(QQQM_PUT, -0.15, 250),
    SPXW_PUT.key: quote(SPXW_PUT, -0.14, 6600),
}

# Fully invested: QQQM $150k (15%), SPY $268k (26.8%, already above the 15% cap).
HOLDINGS = [Position(QQQM, 600, 180.0, 250.0), Position(SPY, 400, 520.0, 670.0)]
CONFIG = TradingConfig()


def intent(contract, action=Action.SELL, qty=1, risk_reducing=False):
    return TradeIntent(
        "i",
        "s",
        IntentKind.OPEN,
        (OrderLeg(contract, action, qty),),
        "T",
        NOW,
        risk_reducing,
    )


def ctx(
    positions=HOLDINGS, el_after=450_000, orders_today=0, config=CONFIG, exposures=None
):
    account = AccountSnapshot("DU1", NOW, NLV, 500_000, 300_000, 380_000, 420_000)
    return LimitContext.from_config(
        config,
        account=account,
        impact=MarginImpact(el_after + 300_000, 380_000, 300_000),
        exposures=exposures
        if exposures is not None
        else exposures_from(positions, QUOTES),
        quotes=QUOTES,
        opening_orders_today=orders_today,
    )


def failures(intent_, context):
    return {c.name for c in gate_decision(intent_, context, CONFIG).failures}


def test_xsp_put_on_fully_invested_account_passes():
    d = gate_decision(intent(xsp_put()), ctx(), CONFIG)
    assert d.approved, d.summary()


@pytest.mark.parametrize(
    "el_after,failed",
    [(124_999, {"el_after", "cushion_after"}), (390_000, {"cushion_after"})],
)
def test_margin_after_trade(el_after, failed):
    assert failures(intent(xsp_put()), ctx(el_after=el_after)) == failed


def test_short_put_notional_cap():
    # Existing: 8 x 600 strike = $480k in Dec+Jan; one more ($60k) breaks the $500k cap.
    puts = [
        Position(xsp_put(expiry=DEC), -2, 3.0),
        Position(xsp_put(expiry=JAN), -6, 3.0),
    ]
    f = failures(intent(xsp_put(expiry=DEC)), ctx(HOLDINGS + puts))
    assert "short_put_notional" in f and "index_put_notional" in f


def test_index_put_cap_is_separate():
    # $360k of index puts (6 x 600 x 100) + a 400-strike put ($40k) = $400k: at the cap.
    puts = [Position(xsp_put(expiry=JAN), -6, 3.0)]
    assert "index_put_notional" not in failures(
        intent(OptionContract("XSP", DEC, 400, Right.PUT)), ctx(HOLDINGS + puts)
    )
    # ...but one more 600-strike XSP put ($420k) breaks it.
    assert "index_put_notional" in failures(intent(xsp_put()), ctx(HOLDINGS + puts))


def test_expiry_ladder_measured_against_cap():
    # Cap per expiry = 35% x $500k = $175k. First put ($60k) is fine.
    assert "expiry_ladder" not in failures(intent(xsp_put()), ctx())
    two = [Position(xsp_put(), -2, 3.0)]  # $120k in Dec
    assert "expiry_ladder" in failures(intent(xsp_put()), ctx(HOLDINGS + two))  # $180k
    assert "expiry_ladder" not in failures(
        intent(xsp_put(expiry=JAN)), ctx(HOLDINGS + two)
    )


def test_per_name_blocks_increases_only():
    # SPY is already $268k (> $150k cap): selling a SPY put adds exposure -> blocked.
    assert "per_name" in failures(intent(SPY_PUT), ctx())
    # A covered call reduces SPY exposure -> allowed even though SPY is above the cap.
    assert "per_name" not in failures(intent(SPY_CALL), ctx())
    # QQQM is exactly at 15%: a QQQM put pushes it over.
    assert "per_name" in failures(intent(QQQM_PUT), ctx())


def test_per_sector_uses_watchlist_sectors():
    cfg = TradingConfig.model_validate(
        {"watchlist": {"QQQM": {"sector": "tech"}, "AAPL": {"sector": "tech"}}}
    )
    aapl = StockContract("AAPL")
    held = HOLDINGS + [Position(aapl, 900, 150.0, 230.0)]  # tech = 150k + 207k = 357k
    aapl_put = OptionContract("AAPL", DEC, 210, Right.PUT)
    context = ctx(held, config=cfg)
    context.quotes = {**QUOTES, aapl_put.key: quote(aapl_put, -0.15, 230)}
    decision = gate_decision(intent(aapl_put), context, cfg)
    assert "per_sector" in {c.name for c in decision.failures}


def test_options_beta_delta_cap():
    # Options-only delta: 0.35 x $1M = $350k cap. Start just under it.
    # 0.15 x 100 x 35 x 660 = $346.5k of options delta, just under the cap.
    near_cap = [Exposure(xsp_put(expiry=JAN), -35, -0.15, 660)]
    exp = exposures_from(HOLDINGS, QUOTES) + near_cap
    assert "options_beta_delta" in failures(intent(xsp_put()), ctx(exposures=exp))
    # A covered call lowers options delta: allowed even above the cap.
    assert "options_beta_delta" not in failures(intent(SPY_CALL), ctx(exposures=exp))


@pytest.mark.parametrize(
    "contract,qty,ok",
    [
        (xsp_put(), 20, True),
        (xsp_put(), 21, False),
        (SPXW_PUT, 5, True),
        (SPXW_PUT, 6, False),
        (QQQM_PUT, 10, True),
        (QQQM_PUT, 11, False),
    ],
)
def test_contracts_per_order(contract, qty, ok):
    assert (
        "contracts_per_order" not in failures(intent(contract, qty=qty), ctx())
    ) is ok


def test_orders_per_day():
    assert "orders_per_day" in failures(intent(xsp_put()), ctx(orders_today=5))
    assert "orders_per_day" not in failures(intent(xsp_put()), ctx(orders_today=4))


def test_risk_reducing_skips_limits():
    close = intent(xsp_put(), Action.BUY, qty=50, risk_reducing=True)
    d = gate_decision(close, ctx(el_after=10_000, orders_today=99), CONFIG)
    assert d.approved and len(d.checks) == 1


def test_option_without_greeks_counted_at_full_delta():
    naked = OptionContract("ZZZ", DEC, 50, Right.PUT)
    [e] = exposures_from([Position(naked, -1, 1.0)], {})
    assert e.delta == -1.0 and e.delta_dollars == pytest.approx(5_000)
