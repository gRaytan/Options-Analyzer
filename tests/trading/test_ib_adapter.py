"""IBAdapter connection tests with a simulated ib_async.IB (plan T06)."""
import asyncio

import pytest
from eventkit import Event

from src.trading.broker.base import BrokerError, ReadOnlyError
from src.trading.broker.ib_adapter import (
    AccountMismatch,
    IBAdapter,
    IBConnectionSettings,
    LiveTradingNotAllowed,
    is_paper_account,
)
from src.trading.domain import Action, OptionContract, OrderLeg, OrderRequest, Right
from datetime import date


class FakeIB:
    """The subset of ib_async.IB the adapter uses in T06."""

    def __init__(self, accounts=("DU123",), fail_times=0):
        self.accounts = list(accounts)
        self.fail_times = fail_times
        self.connected = False
        self.connect_calls = []
        self.disconnectedEvent = Event("disconnectedEvent")

    async def connectAsync(self, host, port, clientId, timeout, readonly, account):
        self.connect_calls.append(
            dict(
                host=host,
                port=port,
                clientId=clientId,
                readonly=readonly,
                account=account,
            )
        )
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionRefusedError("refused")
        self.connected = True

    def managedAccounts(self):
        return self.accounts

    def isConnected(self):
        return self.connected

    def disconnect(self):
        if self.connected:
            self.connected = False
            self.disconnectedEvent.emit()

    def drop(self):
        """Simulate the Gateway going away (e.g. the nightly restart)."""
        self.connected = False
        self.disconnectedEvent.emit()


async def no_sleep(_):
    await asyncio.sleep(0)


def make(account="DU123", port=4002, read_only=True, ib=None, **kw):
    ib = ib or FakeIB()
    adapter = IBAdapter(
        account,
        IBConnectionSettings(port=port, client_id=7),
        read_only=read_only,
        ib_factory=lambda: ib,
        sleep=no_sleep,
        **kw,
    )
    return adapter, ib


def test_connect_read_only_by_default():
    adapter, ib = make()
    events = []
    adapter.subscribe("connection", events.append)
    asyncio.run(adapter.connect())
    assert adapter.is_connected
    assert ib.connect_calls[0] == dict(
        host="127.0.0.1", port=4002, clientId=7, readonly=True, account="DU123"
    )
    assert events == [True]
    assert adapter.health.managed_accounts == ["DU123"]


def test_account_mismatch_refuses_and_disconnects():
    adapter, ib = make(account="DU999")
    with pytest.raises(AccountMismatch, match="DU123"):
        asyncio.run(adapter.connect())
    assert not ib.connected and not adapter.is_connected


def test_connection_failure_is_broker_error():
    adapter, _ = make(ib=FakeIB(fail_times=1))
    with pytest.raises(BrokerError, match="cannot connect"):
        asyncio.run(adapter.connect())
    assert "refused" in adapter.health.last_error


@pytest.mark.parametrize(
    "account,port",
    [("U123", 4002), ("DU123", 4001), ("U123", 7496)],
)
def test_live_requires_read_only(account, port):
    with pytest.raises(LiveTradingNotAllowed):
        make(account=account, port=port, read_only=False)
    adapter, _ = make(account=account, port=port, read_only=True)
    assert adapter.is_live


def test_paper_can_be_opened_for_trading():
    adapter, ib = make(read_only=False)
    asyncio.run(adapter.connect())
    assert ib.connect_calls[0]["readonly"] is False
    assert not adapter.is_live


def test_read_only_blocks_place_order():
    adapter, _ = make()
    opt = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)
    order = OrderRequest("r", (OrderLeg(opt, Action.SELL, 1),), -1.0, "r")
    with pytest.raises(ReadOnlyError):
        asyncio.run(adapter.place_order(order))


def test_reconnects_with_backoff_after_drop():
    ib = FakeIB()
    delays = []

    async def record_sleep(d):
        delays.append(d)
        await asyncio.sleep(0)

    adapter = IBAdapter(
        "DU123", IBConnectionSettings(), ib_factory=lambda: ib, sleep=record_sleep
    )
    events = []
    adapter.subscribe("connection", events.append)

    async def scenario():
        await adapter.connect()
        ib.fail_times = 3  # Gateway restarting: three refused attempts
        ib.drop()
        await adapter.wait_reconnected()

    asyncio.run(scenario())
    assert adapter.is_connected
    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert events == [True, False, True]
    assert adapter.health.reconnect_attempts == 0  # reset after success


def test_backoff_caps_at_max():
    ib = FakeIB()
    delays = []

    async def record_sleep(d):
        delays.append(d)
        await asyncio.sleep(0)

    adapter = IBAdapter(
        "DU123",
        IBConnectionSettings(reconnect_max_s=5),
        ib_factory=lambda: ib,
        sleep=record_sleep,
    )

    async def scenario():
        await adapter.connect()
        ib.fail_times = 5
        ib.drop()
        await adapter.wait_reconnected()

    asyncio.run(scenario())
    assert delays == [1.0, 2.0, 4.0, 5, 5, 5]


def test_user_disconnect_does_not_reconnect():
    adapter, ib = make()

    async def scenario():
        await adapter.connect()
        await adapter.disconnect()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert not adapter.is_connected
    assert len(ib.connect_calls) == 1


def test_settings_from_env():
    s = IBConnectionSettings.from_env(
        {"IB_HOST": "gw", "IB_PORT": "4001", "IB_CLIENT_ID": "2"}
    )
    assert (s.host, s.port, s.client_id) == ("gw", 4001, 2)
    assert not s.is_paper_port
    assert IBConnectionSettings.from_env({}).port == 4002


def test_paper_account_detection():
    assert is_paper_account("DU1234567")
    assert not is_paper_account("U1234567")


def test_account_required():
    with pytest.raises(ValueError):
        IBAdapter("", ib_factory=FakeIB)
