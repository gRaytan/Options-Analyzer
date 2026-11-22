"""Fee and financing model (plan T21)."""
from datetime import date, datetime, timezone

import pytest

from src.trading.config import FeesConfig, TradingConfig
from src.trading.domain import (
    Action,
    Fill,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Right,
    StockContract,
)
from src.trading.fees import (
    FeeCalibrator,
    commission_per_contract,
    estimate_order_fees,
    margin_interest,
    net_credit,
)

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
CFG = FeesConfig()
QQQM_CALL = OptionContract("QQQM", date(2026, 12, 18), 270, Right.CALL)
XSP_PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT)
SPXW_PUT = OptionContract(
    "SPX", date(2026, 12, 31), 6000, Right.PUT, trading_class="SPXW"
)


def order(*legs, limit=-1.0):
    return OrderRequest("r", tuple(legs), limit, "r")


@pytest.mark.parametrize(
    "premium,fee", [(0.03, 0.25), (0.05, 0.50), (0.09, 0.50), (0.10, 0.65), (4.2, 0.65)]
)
def test_commission_tiers(premium, fee):
    assert commission_per_contract(premium, CFG) == fee


def test_single_leg_estimate():
    est = estimate_order_fees(
        order(OrderLeg(QQQM_CALL, Action.SELL, 3)), {QQQM_CALL.key: 0.80}
    )
    assert est.commission == pytest.approx(3 * 0.65)
    assert est.exchange == pytest.approx(3 * 0.20)
    assert est.regulatory == pytest.approx(3 * 0.05)
    assert est.total == pytest.approx(3 * 0.90)
    assert not est.calibrated


def test_minimum_per_order():
    est = estimate_order_fees(
        order(OrderLeg(QQQM_CALL, Action.BUY, 1)), {QQQM_CALL.key: 0.03}
    )
    assert est.commission == 1.00  # 0.25 raised to the $1 minimum


def test_index_exchange_fees():
    xsp = estimate_order_fees(order(OrderLeg(XSP_PUT, Action.SELL, 2)))
    spx = estimate_order_fees(order(OrderLeg(SPXW_PUT, Action.SELL, 2)))
    assert xsp.exchange == pytest.approx(0.20) and spx.exchange == pytest.approx(1.20)


def test_roll_counts_both_legs():
    later = OptionContract("XSP", date(2027, 1, 29), 590, Right.PUT)
    est = estimate_order_fees(
        order(OrderLeg(XSP_PUT, Action.BUY, 1), OrderLeg(later, Action.SELL, 1))
    )
    assert est.commission == pytest.approx(1.30)


def test_calibrator_replaces_estimate_after_enough_fills():
    cal = FeeCalibrator(FeesConfig(calibration_min_fills=3))
    fills = [
        Fill(f"e{i}", "r", XSP_PUT, Action.SELL, 2, 1.0, T0, commission=1.70)
        for i in range(2)
    ]
    cal.add_fills(fills)
    assert cal.per_contract("XSP") is None  # only 2 fills
    cal.add_fills([Fill("e9", "r", XSP_PUT, Action.SELL, 2, 1.0, T0, commission=1.70)])
    assert cal.per_contract("xsp") == pytest.approx(0.85)
    est = estimate_order_fees(order(OrderLeg(XSP_PUT, Action.SELL, 4)), calibrator=cal)
    assert est.calibrated and est.total == pytest.approx(3.40)
    # Other roots still use the estimate.
    assert not estimate_order_fees(
        order(OrderLeg(QQQM_CALL, Action.SELL, 1)), calibrator=cal
    ).calibrated


def test_net_credit():
    o = order(OrderLeg(QQQM_CALL, Action.SELL, 2), limit=-0.80)  # $160 gross
    assert net_credit(o, {QQQM_CALL.key: 0.80}) == pytest.approx(160 - 2 * 0.90)
    debit = order(OrderLeg(QQQM_CALL, Action.BUY, 1), limit=0.40)
    assert net_credit(debit, {QQQM_CALL.key: 0.40}) == pytest.approx(-40 - 1.25)


def test_stock_commission_and_margin_interest():
    est = estimate_order_fees(
        order(OrderLeg(StockContract("QQQM"), Action.BUY, 100), limit=250)
    )
    assert est.commission == 1.00  # $0.35 raised to the order minimum
    assert margin_interest(-36_000, 30) == pytest.approx(36_000 * 0.06 * 30 / 360)
    assert margin_interest(5_000, 30) == 0.0


def test_config_defaults_present():
    assert dict(TradingConfig().fees.index_exchange_per_contract).get("XSP") == 0.10
