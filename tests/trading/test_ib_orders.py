"""IB order placement (plan T22b) against a simulated ib_async.IB."""
import asyncio
from datetime import date, datetime, timezone

import pytest
from eventkit import Event
from ib_async.objects import CommissionReport, Execution
from ib_async.objects import Fill as IBFill
from ib_async.order import OrderStatus as IBOrderStatus
from ib_async.order import Trade, TradeLogEntry

from src.trading.broker.base import OrderRejected, OrderStatus, ReadOnlyError
from src.trading.broker.ib_adapter import IBAdapter
from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Quote,
    Right,
    TradeIntent,
)
from src.trading.journal import Journal
from src.trading.order_manager import ExecStatus, OrderManager
from src.trading.risk.checks import CheckResult, GateDecision
from tests.trading.test_what_if import WhatIfIB

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT, trading_class="XSP")
PUT2 = OptionContract("XSP", date(2027, 1, 29), 590, Right.PUT, trading_class="XSP")


class OrdersIB(WhatIfIB):
    def __init__(self, fill_on_place=False):
        super().__init__()
        self.orderStatusEvent = Event("orderStatusEvent")
        self.commissionReportEvent = Event("commissionReportEvent")
        self.trades = []
        self.place_calls = []
        self.cancelled = []
        self._fills = []
        self.next_id = 100
        self.fill_on_place = fill_on_place

    def placeOrder(self, contract, order):
        self.place_calls.append((contract, order, order.lmtPrice))
        if not order.orderId:
            self.next_id += 1
            order.orderId = self.next_id
        for t in self.trades:
            if t.order.orderId == order.orderId:
                return t  # modification
        trade = Trade(
            contract,
            order,
            IBOrderStatus(order.orderId, "Submitted", 0, order.totalQuantity),
        )
        self.trades.append(trade)
        if self.fill_on_place:
            self.fill(trade, abs(order.lmtPrice))
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        for t in self.trades:
            if t.order.orderId == order.orderId:
                t.orderStatus.status = "Cancelled"

    def openTrades(self):
        return [
            t
            for t in self.trades
            if t.orderStatus.status not in IBOrderStatus.DoneStates
        ]

    def fills(self):
        return list(self._fills)

    def fill(self, trade, price, commission=1.30):
        legs = [trade.contract]
        if trade.contract.secType == "BAG":
            legs = []  # per-leg executions plus a BAG summary
        ex = Execution(
            execId=f"x{len(self._fills)}",
            time=NOW,
            acctNumber="DU123",
            side="SLD" if trade.order.action == "SELL" else "BOT",
            shares=trade.order.totalQuantity,
            price=price,
            orderRef=trade.order.orderRef,
        )
        report = CommissionReport(execId=ex.execId, commission=commission)
        f = IBFill(trade.contract, ex, report, NOW)
        self._fills.append(f)
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = trade.order.totalQuantity
        trade.orderStatus.avgFillPrice = price
        self.commissionReportEvent.emit(trade, f, report)
        self.orderStatusEvent.emit(trade)


def adapter(ib, read_only=False):
    async def no_sleep(_):
        return None

    a = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep, read_only=read_only)
    asyncio.run(a.connect())
    return a


SELL = OrderRequest("ref-1", (OrderLeg(PUT, Action.SELL, 2),), -1.05, "ref-1")


def run(c):
    return asyncio.run(c)


def test_read_only_adapter_cannot_place_or_modify():
    a = adapter(OrdersIB(), read_only=True)
    with pytest.raises(ReadOnlyError):
        run(a.place_order(SELL))
    with pytest.raises(ReadOnlyError):
        run(a.modify_order("ref-1", -1.04))


def test_place_single_leg():
    ib = OrdersIB()
    a = adapter(ib)
    state = run(a.place_order(SELL))
    contract, order, price = ib.place_calls[0]
    assert (order.action, order.totalQuantity, price) == ("SELL", 2, 1.05)
    assert order.orderRef == "ref-1" and order.account == "DU123" and not order.whatIf
    assert state.status is OrderStatus.WORKING
    with pytest.raises(OrderRejected):
        run(a.place_order(SELL))


def test_modify_reuses_order_id():
    ib = OrdersIB()
    a = adapter(ib)
    run(a.place_order(SELL))
    run(a.modify_order("ref-1", -1.03))
    (_, o1, p1), (_, o2, p2) = ib.place_calls
    assert o1.orderId == o2.orderId and (p1, p2) == (1.05, 1.03)


def test_fill_events_status_and_executions():
    ib = OrdersIB()
    a = adapter(ib)
    fills, orders = [], []
    a.subscribe("fill", fills.append)
    a.subscribe("order", orders.append)
    run(a.place_order(SELL))
    ib.fill(ib.trades[0], 1.04, commission=1.70)
    [f] = fills
    assert (f.order_ref, f.action, f.quantity, f.price, f.commission) == (
        "ref-1",
        Action.SELL,
        2,
        1.04,
        1.70,
    )
    assert f.contract.key == PUT.key
    assert orders[-1].status is OrderStatus.FILLED and orders[-1].avg_price == -1.04
    assert run(a.executions()) == [f]
    assert run(a.open_orders()) == []


def test_cancel():
    ib = OrdersIB()
    a = adapter(ib)
    run(a.place_order(SELL))
    state = run(a.cancel_order("ref-1"))
    assert ib.cancelled and state.status is OrderStatus.CANCELLED


def test_rejected_order_reports_ib_message():
    ib = OrdersIB()
    a = adapter(ib)
    run(a.place_order(SELL))
    t = ib.trades[0]
    t.orderStatus.status = "Inactive"
    t.log.append(TradeLogEntry(NOW, "Inactive", "Order rejected: insufficient margin"))
    [state] = [run(a.cancel_order("ref-1"))]
    assert (
        state.status is OrderStatus.REJECTED and "insufficient margin" in state.reason
    )


def test_open_orders_after_restart_are_rebuilt():
    ib = OrdersIB()
    run(adapter(ib).place_order(SELL))
    fresh = adapter(ib)  # new process, same Gateway session
    [state] = run(fresh.open_orders())
    assert state.order.order_ref == "ref-1"
    assert (
        state.order.legs[0].contract.key == PUT.key
        and state.order.legs[0].quantity == 2
    )


def test_combo_open_order_legs_rebuilt_from_cache():
    ib = OrdersIB()
    a = adapter(ib)
    roll = OrderRequest(
        "roll-1",
        (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 1)),
        -0.4,
        "roll-1",
    )
    run(a.place_order(roll))
    a._orders.clear()  # forget it: as if placed before a restart  # noqa: SLF001
    [state] = run(a.open_orders())
    assert {leg.contract.key for leg in state.order.legs} == {PUT.key, PUT2.key}


def test_order_manager_end_to_end_with_ib_adapter(tmp_path):
    ib = OrdersIB(fill_on_place=True)
    a = adapter(ib)
    journal = Journal(tmp_path / "j.db")

    async def quotes_fn(contracts):
        return {c.key: Quote(c, 1.00, 1.10, NOW) for c in contracts}

    async def gate(order, intent, quotes):
        return GateDecision((CheckResult("ok", True),))

    async def no_sleep(_):
        return None

    om = OrderManager(a, journal, sleep=no_sleep)
    intent = TradeIntent(
        "i-1", "index_put", IntentKind.OPEN, (OrderLeg(PUT, Action.SELL, 2),), "T", NOW
    )
    res = run(om.execute(intent, quotes_fn, gate))
    assert res.status is ExecStatus.FILLED
    [rec] = journal.read("fills")
    assert rec.data.order_ref == "i-1" and rec.data.commission == 1.30
