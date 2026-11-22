"""FakeAdapter: interface suite plus fake-specific behavior (plan T05)."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from src.trading.broker import (
    BrokerDisconnected,
    OptionChain,
    OrderRejected,
    OrderStatus,
)
from src.trading.broker.fake import FakeAdapter
from src.trading.domain import (
    Action,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Quote,
    Right,
    StockContract,
)
from tests.trading.broker_contract import BrokerContract

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)
PUT2 = OptionContract("XSP", date(2027, 1, 29), 540, Right.PUT)
CALL = OptionContract("QQQM", date(2026, 12, 18), 240, Right.CALL)


def run(coro):
    return asyncio.run(coro)


def quote(contract, bid, ask):
    return Quote(contract, bid, ask, T0, underlying_price=600)


class TestFakeAdapterContract(BrokerContract):
    def make_adapter(self, read_only):
        return FakeAdapter(read_only=read_only, clock=lambda: T0)

    def prepare(self, adapter):
        adapter.set_quote(quote(PUT, 1.00, 1.10))
        return PUT


@pytest.fixture
def broker():
    adapter = FakeAdapter(clock=lambda: T0)
    run(adapter.connect())
    adapter.set_quote(quote(PUT, 1.00, 1.10))
    adapter.set_quote(quote(PUT2, 1.50, 1.62))
    adapter.set_quote(quote(CALL, 0.40, 0.60))
    return adapter


def sell_put(limit, qty=1, ref="o1"):
    return OrderRequest(ref, (OrderLeg(PUT, Action.SELL, qty),), limit, ref)


def test_marketable_sell_fills_and_updates_position_and_cash(broker):
    fills = []
    broker.subscribe("fill", fills.append)
    state = run(broker.place_order(sell_put(-1.00, qty=2)))
    assert state.status is OrderStatus.FILLED
    assert state.avg_price == -1.00
    [pos] = run(broker.positions())
    assert pos.quantity == -2 and pos.avg_cost == 1.00
    # Credit 2 x 100 x $1.00 minus 2 x $0.65 commission.
    assert run(broker.account_snapshot()).cash == pytest.approx(200 - 1.30)
    assert len(fills) == 1 and fills[0].commission == pytest.approx(1.30)


def test_partial_fill_then_complete(broker):
    broker.auto_fill = False
    run(broker.place_order(sell_put(-1.05, qty=3)))
    partial = broker.fill_order("o1", 1, -1.05)
    assert partial.status is OrderStatus.PARTIAL and partial.remaining == 2
    done = broker.fill_order("o1", 5, -1.04)
    assert done.status is OrderStatus.FILLED and done.filled == 3
    assert done.avg_price == pytest.approx((-1.05 - 1.04 * 2) / 3)
    assert run(broker.positions())[0].quantity == -3


def test_closing_removes_position(broker):
    run(broker.place_order(sell_put(-1.00)))
    buy = OrderRequest("o2", (OrderLeg(PUT, Action.BUY, 1),), 1.10, "o2")
    assert run(broker.place_order(buy)).status is OrderStatus.FILLED
    assert run(broker.positions()) == []


def test_roll_as_one_combo(broker):
    run(broker.place_order(sell_put(-1.00)))
    # Buy back 550 (ask 1.10), sell 540 (bid 1.50): natural net = -0.40 credit.
    roll = OrderRequest(
        "roll1",
        (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 1)),
        -0.40,
        "roll1",
    )
    assert run(broker.place_order(roll)).status is OrderStatus.FILLED
    [pos] = run(broker.positions())
    assert pos.contract == PUT2 and pos.quantity == -1
    too_greedy = OrderRequest(
        "roll2",
        (OrderLeg(PUT2, Action.BUY, 1), OrderLeg(PUT, Action.SELL, 1)),
        -1.0,
        "roll2",
    )
    assert run(broker.place_order(too_greedy)).status is OrderStatus.WORKING


def test_reject_duplicate_and_unequal_combos(broker):
    broker.auto_fill = False
    run(broker.place_order(sell_put(-1.05)))
    with pytest.raises(OrderRejected):
        run(broker.place_order(sell_put(-1.05)))
    unequal = OrderRequest(
        "u", (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 2)), 0, "u"
    )
    with pytest.raises(OrderRejected):
        run(broker.place_order(unequal))
    broker.reject_next_order("no margin")
    state = run(broker.place_order(sell_put(-1.0, ref="o9")))
    assert state.status is OrderStatus.REJECTED and state.reason == "no margin"


def test_drop_connection_blocks_calls(broker):
    events = []
    broker.subscribe("connection", events.append)
    broker.drop_connection()
    assert events == [False]
    with pytest.raises(BrokerDisconnected):
        run(broker.positions())


def test_put_assignment_delivers_shares(broker):
    broker.set_position(PUT, -1, 1.0)
    broker.assign(PUT, 1)
    positions = {p.contract.key: p for p in run(broker.positions())}
    assert PUT.key not in positions
    assert positions["XSP"].quantity == 100
    assert run(broker.account_snapshot()).cash == pytest.approx(-55_000)


def test_call_assignment_removes_shares(broker):
    broker.set_position(StockContract("QQQM"), 100, 200.0)
    broker.set_position(CALL, -1, 0.5)
    broker.assign(CALL, 1)
    assert run(broker.positions()) == []
    assert run(broker.account_snapshot()).cash == pytest.approx(24_000)


def test_executions_since_and_chain(broker):
    run(broker.place_order(sell_put(-1.00)))
    assert len(run(broker.executions())) == 1
    assert run(broker.executions(since=datetime(2030, 1, 1, tzinfo=timezone.utc))) == []
    chain = OptionChain("XSP", "XSP", 100, (date(2026, 12, 31),), (540.0, 550.0))
    broker.set_chain("XSP", [chain])
    assert run(broker.option_chain("XSP")) == [chain]


def test_set_account_publishes_and_what_if_adds_margin(broker):
    events = []
    broker.subscribe("account", events.append)
    broker.set_account(excess_liquidity=120_000)
    assert events[-1].excess_liquidity == 120_000
    impact = run(broker.what_if(sell_put(-1.0)))
    assert impact.maint_margin_after == pytest.approx(300_000 + 0.15 * 550 * 100)
