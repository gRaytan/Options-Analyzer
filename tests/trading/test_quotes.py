"""Option chains, quotes and pacing (plan T09)."""
import asyncio
from datetime import date, datetime, timezone

import pytest
from eventkit import Event
from ib_async.contract import Contract as IBContract
from ib_async.objects import OptionChain as IBChain
from ib_async.objects import OptionComputation
from ib_async.ticker import Ticker

from src.trading.broker.base import BrokerDisconnected, BrokerError
from src.trading.broker.ib_adapter import (
    IBAdapter,
    RateLimiter,
    contract_to_ib,
    underlying_to_ib,
)
from src.trading.domain import OptionContract, Right, StockContract

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
XSP_PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT, trading_class="XSP")
QQQM = StockContract("QQQM")


class QuoteIB:
    def __init__(self):
        self.connected = False
        self.disconnectedEvent = Event("d")
        self.accountValueEvent = Event("a")
        self.next_con_id = 1000
        self.tickers = {}  # contract key-ish (symbol, strike, right) -> Ticker kwargs
        self.qualify_calls = 0
        self.ticker_batches = []
        self.unknown = set()
        self.market_data_type = None

    async def connectAsync(self, *a, **k):
        self.connected = True

    def reqMarketDataType(self, t):
        self.market_data_type = t

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
        self.qualify_calls += 1
        if contract.symbol in self.unknown:
            return [None]
        self.next_con_id += 1
        contract.conId = self.next_con_id
        if not contract.secType:
            contract.secType = "OPT"
        return [contract]

    async def reqSecDefOptParamsAsync(self, symbol, exchange, sec_type, con_id):
        self.secdef_args = (symbol, exchange, sec_type, con_id)
        return [
            IBChain(
                "SMART", con_id, "XSP", "100", {"20261231", "20261218"}, {560, 550}
            ),
            IBChain("CBOE", con_id, "XSP", "100", {"20261231"}, {550}),
            IBChain("SMART", con_id, "XSPW", "100", {"20261204"}, {555}),
        ]

    async def reqTickersAsync(self, *contracts):
        self.ticker_batches.append(len(contracts))
        out = []
        for c in contracts:
            ticker = Ticker(contract=c, time=T0)
            # Ticker resets price fields in __post_init__, so set them afterwards.
            for field, value in self.tickers.get(
                (c.symbol, c.strike, c.right), {}
            ).items():
                setattr(ticker, field, value)
            out.append(ticker)
        return out


def connected(ib=None):
    ib = ib or QuoteIB()

    async def no_sleep(_):
        return None

    adapter = IBAdapter(
        "DU123", ib_factory=lambda: ib, sleep=no_sleep, clock=lambda: T0
    )
    asyncio.run(adapter.connect())
    return adapter, ib


def test_connect_requests_live_market_data():
    _, ib = connected()
    assert ib.market_data_type == 1


def test_contract_to_ib():
    opt = contract_to_ib(XSP_PUT)
    assert (opt.symbol, opt.lastTradeDateOrContractMonth, opt.strike, opt.right) == (
        "XSP",
        "20261231",
        550,
        "P",
    )
    assert (
        opt.exchange == "SMART"
        and opt.tradingClass == "XSP"
        and opt.multiplier == "100"
    )
    stk = contract_to_ib(QQQM)
    assert (stk.secType, stk.symbol, stk.exchange) == ("STK", "QQQM", "SMART")
    idx = underlying_to_ib("xsp")
    assert (idx.secType, idx.symbol, idx.exchange) == ("IND", "XSP", "CBOE")
    assert underlying_to_ib("QQQM").secType == "STK"


def test_option_chain_uses_smart_rows_per_trading_class():
    adapter, ib = connected()
    chains = asyncio.run(adapter.option_chain("XSP"))
    assert ib.secdef_args[0:3] == ("XSP", "", "IND")
    assert [c.trading_class for c in chains] == ["XSP", "XSPW"]
    xsp = chains[0]
    assert xsp.expiries == (date(2026, 12, 18), date(2026, 12, 31))
    assert xsp.strikes == (550.0, 560.0)
    assert xsp.multiplier == 100


def test_quote_with_model_greeks():
    adapter, ib = connected()
    ib.tickers[("XSP", 550, "P")] = dict(
        bid=1.05,
        ask=1.15,
        last=1.10,
        marketDataType=1,
        modelGreeks=OptionComputation(
            0, 0.19, -0.14, 1.1, 0.0, 0.004, 0.32, -0.05, 600.0
        ),
    )
    q = asyncio.run(adapter.quote(XSP_PUT))
    assert q.contract == XSP_PUT
    assert (q.bid, q.ask, q.last) == (1.05, 1.15, 1.10)
    assert q.mid == pytest.approx(1.10)
    assert q.greeks.delta == -0.14 and q.greeks.iv == 0.19
    assert q.underlying_price == 600.0
    assert q.ts == T0 and not q.delayed


def test_missing_values_and_delayed_data():
    adapter, ib = connected()
    ib.tickers[("XSP", 550, "P")] = dict(bid=float("nan"), ask=-1.0, marketDataType=3)
    q = asyncio.run(adapter.quote(XSP_PUT))
    assert q.bid is None and q.ask is None and q.mid is None
    assert q.greeks is None
    assert q.delayed


def test_quotes_batched_and_contracts_cached():
    adapter, ib = connected()
    adapter.quote_batch_size = 2
    contracts = [
        OptionContract("XSP", date(2026, 12, 31), k, Right.PUT)
        for k in (540, 545, 550, 555, 560)
    ]
    quotes = asyncio.run(adapter.quotes(contracts))
    assert [q.contract for q in quotes] == contracts
    assert ib.ticker_batches == [2, 2, 1]
    assert ib.qualify_calls == 5
    asyncio.run(adapter.quotes(contracts))
    assert ib.qualify_calls == 5  # qualified contracts are cached


def test_unknown_contract_raises():
    adapter, ib = connected()
    ib.unknown.add("NOPE")
    with pytest.raises(BrokerError, match="does not recognize"):
        asyncio.run(adapter.quote(StockContract("NOPE")))


def test_quotes_require_connection():
    adapter = IBAdapter("DU123", ib_factory=QuoteIB)
    with pytest.raises(BrokerDisconnected):
        asyncio.run(adapter.quote(QQQM))
    with pytest.raises(BrokerDisconnected):
        asyncio.run(adapter.option_chain("XSP"))


def test_rate_limiter_spaces_requests():
    now = [0.0]
    slept = []

    async def fake_sleep(d):
        slept.append(round(d, 6))
        now[0] += d

    limiter = RateLimiter(rate=10, sleep=fake_sleep, monotonic=lambda: now[0])

    async def scenario():
        await limiter.acquire()  # immediate
        await limiter.acquire()  # waits 0.1 s
        await limiter.acquire(5)  # waits 0.1 s, then reserves 0.5 s
        await limiter.acquire()  # waits 0.5 s

    asyncio.run(scenario())
    assert slept == [0.1, 0.1, 0.5]
