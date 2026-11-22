"""Daily history: IB adapter mapping and cache (plan T10)."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from eventkit import Event
from ib_async.objects import BarData

from src.trading.broker.fake import FakeAdapter
from src.trading.broker.ib_adapter import IBAdapter
from src.trading.domain import Bar, HistoryKind
from src.trading.history import HistoryStore

DAY = date(2026, 11, 20)


def bars(n, start=DAY - timedelta(days=30)):
    return [
        Bar(start + timedelta(days=i), 1 + i, 2 + i, 0.5 + i, 1.5 + i, 100)
        for i in range(n)
    ]


class HistIB:
    def __init__(self):
        self.connected = False
        self.disconnectedEvent = Event("d")
        self.accountValueEvent = Event("a")
        self.calls = []

    async def connectAsync(self, *a, **k):
        self.connected = True

    def reqMarketDataType(self, t):
        pass

    def reqPnL(self, account, modelCode=""):
        from ib_async.objects import PnL

        self.pnl = PnL(account=account)
        return self.pnl

    def managedAccounts(self):
        return ["DU123"]

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def accountValues(self, account=""):
        return []

    async def qualifyContractsAsync(self, contract):
        contract.conId = 1
        return [contract]

    async def reqHistoricalDataAsync(self, contract, **kw):
        self.calls.append((contract.secType, contract.symbol, kw))
        return [
            BarData(
                date=DAY - timedelta(days=1),
                open=1,
                high=2,
                low=0.5,
                close=1.5,
                volume=-1,
            ),
            BarData(date=DAY, open=1.5, high=2.5, low=1, close=2, volume=1000),
        ]


def test_ib_daily_history_request_and_mapping():
    ib = HistIB()

    async def no_sleep(_):
        return None

    adapter = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)

    async def scenario():
        await adapter.connect()
        return await adapter.daily_history("XSP", HistoryKind.IMPLIED_VOL, 365)

    result = asyncio.run(scenario())
    sec_type, symbol, kw = ib.calls[0]
    assert (sec_type, symbol) == ("IND", "XSP")
    assert kw["durationStr"] == "365 D"
    assert kw["barSizeSetting"] == "1 day"
    assert kw["whatToShow"] == "OPTION_IMPLIED_VOLATILITY"
    assert kw["useRTH"] is True
    assert result[0] == Bar(
        DAY - timedelta(days=1), 1, 2, 0.5, 1.5, 0.0
    )  # -1 volume -> 0
    assert result[1].volume == 1000


@pytest.mark.parametrize("days,expected", [(30, "30 D"), (365, "365 D"), (500, "2 Y")])
def test_duration_string(days, expected):
    ib = HistIB()

    async def no_sleep(_):
        return None

    adapter = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)

    async def scenario():
        await adapter.connect()
        await adapter.daily_history("QQQM", HistoryKind.PRICE, days)

    asyncio.run(scenario())
    assert ib.calls[0][2]["durationStr"] == expected
    assert ib.calls[0][0] == "STK"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def setup(tmp_path):
    broker = FakeAdapter()
    asyncio.run(broker.connect())
    broker.set_history("QQQM", HistoryKind.PRICE, bars(10))
    broker.set_history("QQQM", HistoryKind.IMPLIED_VOL, bars(5))
    clock = Clock()
    return broker, HistoryStore(broker, tmp_path, clock), clock, tmp_path


def test_cache_hit_same_day(setup):
    broker, store, _, _ = setup
    first = asyncio.run(store.daily("QQQM", HistoryKind.PRICE, 365))
    second = asyncio.run(store.daily("qqqm", HistoryKind.PRICE, 365))
    assert first == second == bars(10)
    assert broker.history_calls == 1


def test_refetch_next_day(setup):
    broker, store, clock, _ = setup
    asyncio.run(store.daily("QQQM", HistoryKind.PRICE))
    clock.now += timedelta(days=1)
    asyncio.run(store.daily("QQQM", HistoryKind.PRICE))
    assert broker.history_calls == 2


def test_kinds_cached_separately_and_helpers(setup):
    broker, store, _, _ = setup
    assert asyncio.run(store.closes("QQQM")) == [b.close for b in bars(10)]
    assert asyncio.run(store.implied_vols("QQQM")) == [b.close for b in bars(5)]
    assert broker.history_calls == 2


def test_corrupt_cache_refetches(setup):
    broker, store, _, tmp_path = setup
    asyncio.run(store.daily("QQQM", HistoryKind.PRICE))
    [cache_file] = tmp_path.glob("QQQM_price_*.json")
    cache_file.write_text("{not json")
    assert asyncio.run(store.daily("QQQM", HistoryKind.PRICE)) == bars(10)
    assert broker.history_calls == 2
    assert json.loads(cache_file.read_text())["fetched_on"] == "2026-11-20"


def test_fake_history_limits_days(setup):
    broker, _, _, _ = setup
    assert len(asyncio.run(broker.daily_history("QQQM", HistoryKind.PRICE, 3))) == 3
