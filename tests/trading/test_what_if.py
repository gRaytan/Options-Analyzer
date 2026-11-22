"""IB what-if margin and margin-type detection (plan T18b)."""
import asyncio
from datetime import date, datetime, timezone

import pytest
from ib_async.order import OrderState as IBOrderState

from src.trading.broker.base import BrokerError
from src.trading.broker.ib_adapter import IBAdapter
from src.trading.config import MarginType
from src.trading.domain import (
    AccountSnapshot,
    Action,
    MarginImpact,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Right,
)
from src.trading.risk.margin_type import classify_margin_type, reg_t_short_option_margin
from tests.trading.test_quotes import QuoteIB

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT, trading_class="XSP")
PUT2 = OptionContract("XSP", date(2027, 1, 29), 590, Right.PUT, trading_class="XSP")


class WhatIfIB(QuoteIB):
    def __init__(self, result=None):
        super().__init__()
        self.calls = []
        self.result = result or IBOrderState(
            equityWithLoanAfter="800000.5",
            initMarginAfter="390000",
            maintMarginAfter="312000",
            commission=1.05,
        )

    async def whatIfOrderAsync(self, contract, order):
        self.calls.append((contract, order))
        return self.result


def adapter(ib):
    async def no_sleep(_):
        return None

    a = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)  # read-only
    asyncio.run(a.connect())
    return a


def test_single_leg_what_if_on_read_only_connection():
    ib = WhatIfIB()
    a = adapter(ib)
    order = OrderRequest("r1", (OrderLeg(PUT, Action.SELL, 2),), -1.05, "r1")
    impact = asyncio.run(a.what_if(order))
    assert impact == MarginImpact(800000.5, 390000, 312000, commission=1.05)
    assert impact.excess_liquidity_after == pytest.approx(488000.5)
    contract, ib_order = ib.calls[0]
    assert contract.secType == "OPT" and contract.conId
    assert (ib_order.action, ib_order.totalQuantity, ib_order.lmtPrice) == (
        "SELL",
        2,
        1.05,
    )
    assert (
        ib_order.whatIf is True
        and ib_order.orderRef == "r1"
        and ib_order.account == "DU123"
    )


def test_roll_what_if_is_a_bag_at_net_price():
    ib = WhatIfIB()
    a = adapter(ib)
    roll = OrderRequest(
        "r2", (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 1)), -0.4, "r2"
    )
    asyncio.run(a.what_if(roll))
    bag, ib_order = ib.calls[0]
    assert bag.secType == "BAG" and bag.symbol == "XSP" and bag.exchange == "SMART"
    assert [(leg.action, leg.ratio) for leg in bag.comboLegs] == [
        ("BUY", 1),
        ("SELL", 1),
    ]
    assert all(leg.conId for leg in bag.comboLegs)
    assert (ib_order.action, ib_order.totalQuantity, ib_order.lmtPrice) == (
        "BUY",
        1,
        -0.4,
    )


def test_unequal_combo_rejected():
    a = adapter(WhatIfIB())
    bad = OrderRequest(
        "r3", (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 2)), 0.1, "r3"
    )
    with pytest.raises(BrokerError, match="equal"):
        asyncio.run(a.what_if(bad))


def test_missing_what_if_values_raise():
    empty = IBOrderState(warningText="Order would exceed limits")
    a = adapter(WhatIfIB(result=empty))
    order = OrderRequest("r4", (OrderLeg(PUT, Action.SELL, 1),), -1.0, "r4")
    with pytest.raises(BrokerError, match="no what-if result.*exceed"):
        asyncio.run(a.what_if(order))


def test_unset_commission_becomes_none():
    result = IBOrderState(
        equityWithLoanAfter="1",
        initMarginAfter="1",
        maintMarginAfter="1",
        commission=1.7976931348623157e308,
    )
    a = adapter(WhatIfIB(result=result))
    order = OrderRequest("r5", (OrderLeg(PUT, Action.SELL, 1),), -1.0, "r5")
    assert asyncio.run(a.what_if(order)).commission is None


def test_reg_t_formula():
    spy_put = OptionContract("SPY", date(2026, 12, 31), 600, Right.PUT)
    # 20% x 670 - 70 OTM = 64; 10% x 600 = 60 -> 64 + 1.50 premium = 65.50/share
    assert reg_t_short_option_margin(spy_put, 670, 1.50) == pytest.approx(6550)
    # Index uses 15%: 15% x 670 - 70 = 30.5 < 10% x 600 = 60 -> 60 + 1.50
    assert reg_t_short_option_margin(PUT, 670, 1.50) == pytest.approx(6150)
    call = OptionContract("SPY", date(2026, 12, 31), 740, Right.CALL)
    # 20% x 670 - 70 OTM = 64; floor 10% x 670 = 67 -> 67 + 1 = 68
    assert reg_t_short_option_margin(call, 670, 1.0) == pytest.approx(6800)


@pytest.mark.parametrize(
    "margin_increase,expected",
    [
        (6550, MarginType.REG_T),
        (5000, MarginType.REG_T),
        (2500, MarginType.PORTFOLIO),
        (4000, None),
    ],
)
def test_classify_margin_type(margin_increase, expected):
    spy_put = OptionContract("SPY", date(2026, 12, 31), 600, Right.PUT)
    account = AccountSnapshot("DU1", NOW, 1e6, 5e5, 3e5, 380_000, 4e5)
    impact = MarginImpact(8e5, 380_000 + margin_increase, 3e5)
    result = classify_margin_type(account, impact, spy_put, 670, 1.50)
    assert result.detected == expected
    assert "IB margin" in result.summary
