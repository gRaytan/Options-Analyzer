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
        self.accountValueEvent = Event("accountValueEvent")

    def accountValues(self, account=""):
        return []

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

    def reqMarketDataType(self, data_type):
        self.market_data_type = data_type

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


# -- T07: account values -------------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from ib_async.objects import AccountValue  # noqa: E402

from src.trading.broker.base import BrokerDisconnected  # noqa: E402
from src.trading.broker.ib_adapter import (  # noqa: E402
    ACCOUNT_STALE_AFTER_S,
    AccountDataUnavailable,
)

FULL = {
    "NetLiquidation": "1000000",
    "ExcessLiquidity": "450000",
    "MaintMarginReq": "300000",
    "InitMarginReq": "380000",
    "AvailableFunds": "420000",
    "TotalCashValue": "-5000",
}


class AccountIB(FakeIB):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.values = []
        self.accountValueEvent = Event("accountValueEvent")

    def accountValues(self, account=""):
        return [v for v in self.values if not account or v.account == account]

    def push(self, tag, value, currency="USD", account="DU123", model=""):
        av = AccountValue(account, tag, value, currency, model)
        self.values.append(av)
        self.accountValueEvent.emit(av)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def connected_account_adapter():
    ib, clock = AccountIB(), Clock()
    adapter = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep, clock=clock)
    asyncio.run(adapter.connect())
    return adapter, ib, clock


def test_account_snapshot_from_ib_values():
    adapter, ib, _ = connected_account_adapter()
    for tag, value in FULL.items():
        ib.push(tag, value)
    snap = asyncio.run(adapter.account_snapshot())
    assert snap.net_liquidation == 1_000_000
    assert snap.excess_liquidity == 450_000
    assert snap.cash == -5000
    assert snap.cushion == pytest.approx(0.45)


def test_account_snapshot_incomplete_raises():
    adapter, ib, _ = connected_account_adapter()
    ib.push("NetLiquidation", "1000000")
    with pytest.raises(AccountDataUnavailable, match="ExcessLiquidity"):
        asyncio.run(adapter.account_snapshot())


def test_account_snapshot_requires_connection():
    ib = AccountIB()
    adapter = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)
    with pytest.raises(BrokerDisconnected):
        asyncio.run(adapter.account_snapshot())


def test_ignores_other_accounts_models_and_bad_values_and_prefers_base():
    adapter, ib, _ = connected_account_adapter()
    for tag, value in FULL.items():
        ib.push(tag, value)
    ib.push("ExcessLiquidity", "1", account="DU999")
    ib.push("ExcessLiquidity", "2", model="MODEL1")
    ib.push("ExcessLiquidity", "n/a", currency="EUR")
    ib.push("NetLiquidation", "990000", currency="BASE")
    ib.push("NetLiquidation", "5", currency="EUR")
    snap = asyncio.run(adapter.account_snapshot())
    assert snap.excess_liquidity == 450_000
    assert snap.net_liquidation == 990_000


def test_account_events_published_once_complete():
    adapter, ib, _ = connected_account_adapter()
    events = []
    adapter.subscribe("account", events.append)
    tags = list(FULL.items())
    for tag, value in tags[:-1]:
        ib.push(tag, value)
    assert events == []
    ib.push(*tags[-1])
    assert len(events) == 1 and events[0].excess_liquidity == 450_000


def test_account_staleness():
    adapter, ib, clock = connected_account_adapter()
    assert adapter.account_is_stale()
    for tag, value in FULL.items():
        ib.push(tag, value)
    assert asyncio.run(adapter.account_snapshot()).ts == clock.now
    clock.now += timedelta(seconds=ACCOUNT_STALE_AFTER_S - 1)
    assert not adapter.account_is_stale()
    clock.now += timedelta(seconds=2)
    assert adapter.account_is_stale()
    assert adapter.account_age_seconds() == pytest.approx(ACCOUNT_STALE_AFTER_S + 1)


def test_usd_preferred_over_other_currencies_without_base():
    adapter, ib, _ = connected_account_adapter()
    for tag, value in FULL.items():
        ib.push(tag, value)
    ib.push("ExcessLiquidity", "123", currency="EUR")
    assert asyncio.run(adapter.account_snapshot()).excess_liquidity == 450_000
