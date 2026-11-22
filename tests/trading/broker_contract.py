"""Interface tests every BrokerAdapter must pass (plan T05).

Adapters provide a ``make_adapter(read_only)`` factory and a ``prepare``
hook that loads one tradable option with a quote. The same tests then run
against the fake broker now and the IB paper account later (T06+).
"""
import asyncio

import pytest

from src.trading.broker import BrokerDisconnected, OrderStatus, ReadOnlyError
from src.trading.domain import Action, OrderLeg, OrderRequest


def run(coro):
    return asyncio.run(coro)


class BrokerContract:
    """Subclass and implement ``make_adapter`` and ``prepare``."""

    def make_adapter(self, read_only: bool):  # pragma: no cover - abstract
        raise NotImplementedError

    def prepare(self, adapter):  # pragma: no cover - abstract
        """Return an option contract with a live two-sided quote."""
        raise NotImplementedError

    def connected(self, read_only=False):
        adapter = self.make_adapter(read_only)
        run(adapter.connect())
        return adapter

    def test_connect_and_disconnect(self):
        adapter = self.make_adapter(read_only=True)
        events = []
        adapter.subscribe("connection", events.append)
        run(adapter.connect())
        assert adapter.is_connected
        run(adapter.disconnect())
        assert not adapter.is_connected
        assert events == [True, False]

    def test_calls_fail_when_disconnected(self):
        adapter = self.make_adapter(read_only=True)
        with pytest.raises(BrokerDisconnected):
            run(adapter.account_snapshot())

    def test_account_snapshot(self):
        adapter = self.connected(read_only=True)
        snap = run(adapter.account_snapshot())
        assert snap.account_id == adapter.account_id
        assert snap.net_liquidation > 0
        assert 0 <= snap.cushion <= 1

    def test_read_only_blocks_orders_but_allows_what_if(self):
        adapter = self.connected(read_only=True)
        option = self.prepare(adapter)
        order = OrderRequest("r1", (OrderLeg(option, Action.SELL, 1),), -0.01, "r1")
        impact = run(adapter.what_if(order))
        assert impact.maint_margin_after >= 0
        with pytest.raises(ReadOnlyError):
            run(adapter.place_order(order))

    def test_unmarketable_order_works_then_cancels(self):
        adapter = self.connected()
        option = self.prepare(adapter)
        quote = run(adapter.quote(option))
        # Ask for far more credit than the bid: it must not fill.
        order = OrderRequest(
            "r2", (OrderLeg(option, Action.SELL, 1),), -(quote.ask * 10), "r2"
        )
        state = run(adapter.place_order(order))
        assert state.status is OrderStatus.WORKING
        assert [s.order.order_ref for s in run(adapter.open_orders())] == ["r2"]
        cancelled = run(adapter.cancel_order("r2"))
        assert cancelled.status is OrderStatus.CANCELLED
        assert run(adapter.open_orders()) == []
