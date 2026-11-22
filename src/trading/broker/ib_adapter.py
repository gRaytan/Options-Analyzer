"""Interactive Brokers adapter (plan T06+, spec §4.3).

This is the only module that imports ``ib_async``. Implemented so far:

* T06: connect to IB Gateway / TWS, verify the account, reconnect with backoff.
* T07: account values (net liquidation, excess liquidity, margin, Cushion).
* T08: positions (stocks and equity / index options).
* T09: option chains and quotes (live data only, paced).
* T10: daily price and volatility history.
* T18b: what-if margin for single-leg and combo orders (nothing is placed).

Placing orders arrives in T22.

**Account value freshness:** IB pushes account values roughly every
3 minutes, not in real time. Snapshots carry the time of IB's last update,
and :meth:`IBAdapter.account_age_seconds` lets callers judge staleness
against :data:`ACCOUNT_STALE_AFTER_S`.

Safety rules enforced here, independent of the rest of the engine:

* The connection is **read-only by default**. ``ib_async`` is told to
  connect read-only too, so even a bug elsewhere can't send an order.
* A **live** account (live port, or an account ID not starting with ``DU``)
  can only be opened read-only until mode gating (T28) exists.
* The connection is refused if the configured account isn't one of the
  accounts IB reports for this login.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Awaitable, Callable, Dict, Iterable, List, Optional

from src.trading.broker.base import (
    EVENT_ACCOUNT,
    EVENT_CONNECTION,
    BrokerAdapter,
    BrokerDisconnected,
    BrokerError,
    OptionChain,
)
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    Contract,
    MarginImpact,
    OrderRequest,
    ExerciseStyle,
    Greeks,
    HistoryKind,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
    utcnow,
)

log = logging.getLogger(__name__)

PAPER_PORTS = {4002, 7497}  # IB Gateway paper, TWS paper
LIVE_PORTS = {4001, 7496}  # IB Gateway live, TWS live

# Account tag -> AccountSnapshot field. These are IB's "current" values.
ACCOUNT_TAGS = {
    "NetLiquidation": "net_liquidation",
    "ExcessLiquidity": "excess_liquidity",
    "MaintMarginReq": "maint_margin",
    "InitMarginReq": "init_margin",
    "AvailableFunds": "available_funds",
    "TotalCashValue": "cash",
}
# IB refreshes account values about every 180 s; allow some slack.
ACCOUNT_STALE_AFTER_S = 240.0


class AccountDataUnavailable(BrokerError):
    """IB hasn't sent (all) account values yet."""


# European-style, cash-settled index option roots (no early assignment).
EUROPEAN_INDEX_ROOTS = {"SPX", "XSP", "NDX", "XND", "RUT", "MRUT", "VIX", "DJX"}
# Exchange where IB lists each index (for the underlying contract).
INDEX_EXCHANGES = {
    "SPX": "CBOE",
    "XSP": "CBOE",
    "VIX": "CBOE",
    "RUT": "RUSSELL",
    "MRUT": "RUSSELL",
    "NDX": "NASDAQ",
    "XND": "NASDAQ",
    "DJX": "CBOE",
}

MARKET_DATA_LIVE = 1
DELAYED_DATA_TYPES = {3, 4}  # delayed, delayed-frozen


class DelayedDataError(BrokerError):
    """IB served delayed market data; the bot must not trade on it (spec §4.3)."""


class RateLimiter:
    """Simple pacing: at most ``rate`` requests per second (IB allows ~50 msg/s)."""

    def __init__(
        self, rate: float = 40.0, sleep=asyncio.sleep, monotonic=time.monotonic
    ):
        self.interval = 1.0 / rate
        self._sleep = sleep
        self._monotonic = monotonic
        self._next = 0.0

    async def acquire(self, n: int = 1) -> None:
        now = self._monotonic()
        start = max(now, self._next)
        self._next = start + n * self.interval
        if start > now:
            await self._sleep(start - now)


def _num(value) -> Optional[float]:
    """IB uses NaN and -1 for 'no value'."""
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value < 0:
        return None
    return value


UNSET_DOUBLE = 1.7976931348623157e308  # IB's "no value" for doubles


def _num_or_none(value) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or value >= UNSET_DOUBLE:
        return None
    return value


def _ib_float(value) -> float:
    number = _num_or_none(value)
    if number is None:
        raise BrokerError(f"IB what-if field is missing: {value!r}")
    return number


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def contract_to_ib(contract: Contract):
    """Domain contract -> unqualified ``ib_async`` contract."""
    from ib_async import Option, Stock  # pylint: disable=import-outside-toplevel

    if isinstance(contract, StockContract):
        return Stock(contract.symbol, "SMART", contract.currency)
    return Option(
        contract.underlying,
        contract.expiry.strftime("%Y%m%d"),
        contract.strike,
        contract.right.value,
        "SMART",
        multiplier=str(contract.multiplier),
        currency=contract.currency,
        tradingClass=contract.trading_class or "",
    )


def underlying_to_ib(symbol: str, currency: str = "USD"):
    """The underlying of an option root: an index for SPX/XSP/..., else a stock/ETF."""
    from ib_async import Index, Stock  # pylint: disable=import-outside-toplevel

    symbol = symbol.upper()
    if symbol in INDEX_EXCHANGES:
        return Index(symbol, INDEX_EXCHANGES[symbol], currency)
    return Stock(symbol, "SMART", currency)


def contract_from_ib(contract) -> Optional[Contract]:
    """Translate an ``ib_async`` contract to a domain contract.

    Returns ``None`` for instruments the engine doesn't trade (futures,
    bonds, FX, ...), so callers can report them instead of guessing.
    """
    sec_type = getattr(contract, "secType", "")
    currency = getattr(contract, "currency", "") or "USD"
    if sec_type == "STK":
        return StockContract(contract.symbol, currency=currency)
    if sec_type == "OPT":
        expiry_raw = contract.lastTradeDateOrContractMonth[:8]
        expiry = date(int(expiry_raw[:4]), int(expiry_raw[4:6]), int(expiry_raw[6:8]))
        right = Right.CALL if contract.right.upper().startswith("C") else Right.PUT
        european = contract.symbol.upper() in EUROPEAN_INDEX_ROOTS
        return OptionContract(
            underlying=contract.symbol,
            expiry=expiry,
            strike=float(contract.strike),
            right=right,
            multiplier=int(float(contract.multiplier or 100)),
            style=ExerciseStyle.EUROPEAN if european else ExerciseStyle.AMERICAN,
            cash_settled=european,
            trading_class=contract.tradingClass or None,
            currency=currency,
        )
    return None


class AccountMismatch(BrokerError):
    """The login doesn't manage the configured account."""


class LiveTradingNotAllowed(BrokerError):
    """A live account was opened without read-only before mode gating exists."""


@dataclass(frozen=True)
class IBConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    timeout_s: float = 10.0
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 60.0

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "IBConnectionSettings":
        env = os.environ if env is None else env
        return cls(
            host=env.get("IB_HOST", cls.host),
            port=int(env.get("IB_PORT", cls.port)),
            client_id=int(env.get("IB_CLIENT_ID", cls.client_id)),
        )

    @property
    def is_paper_port(self) -> bool:
        return self.port in PAPER_PORTS


def is_paper_account(account_id: str) -> bool:
    """IB paper accounts start with ``DU`` (paper sub-accounts: ``DF``)."""
    return account_id.upper().startswith(("DU", "DF"))


@dataclass
class ConnectionHealth:
    connected: bool = False
    last_connected_at: Optional[datetime] = None
    last_disconnected_at: Optional[datetime] = None
    reconnect_attempts: int = 0
    last_error: str = ""
    managed_accounts: List[str] = field(default_factory=list)


def _default_ib_factory():
    from ib_async import IB  # pylint: disable=import-outside-toplevel

    return IB()


class IBAdapter(BrokerAdapter):
    def __init__(
        self,
        account_id: str,
        settings: IBConnectionSettings = IBConnectionSettings(),
        read_only: bool = True,
        ib_factory: Callable[[], object] = _default_ib_factory,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        if not account_id:
            raise ValueError("account_id is required (set TRADING_ACCOUNT_ID)")
        super().__init__(account_id, read_only)
        live = not is_paper_account(account_id) or not settings.is_paper_port
        if live and not read_only:
            raise LiveTradingNotAllowed(
                "live accounts are read-only until mode gating (plan T28) is in place"
            )
        self.settings = settings
        self.is_live = live
        self.health = ConnectionHealth()
        self.ib = ib_factory()
        self._sleep = sleep
        self._clock = clock
        self._closing = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._account_updated_at: Optional[datetime] = None
        self._pnl = None  # ib_async PnL object, live after connect
        self.unsupported_positions: List[str] = []
        self._qualified: Dict[str, object] = {}
        self.pacer = RateLimiter(sleep=sleep)
        self.quote_batch_size = 50  # stay well under IB's 100 market data lines
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.accountValueEvent += self._on_account_value

    # -- connection -------------------------------------------------------------

    async def connect(self) -> None:
        self._closing = False
        await self._connect_once()

    async def _connect_once(self) -> None:
        s = self.settings
        try:
            await self.ib.connectAsync(
                s.host,
                s.port,
                clientId=s.client_id,
                timeout=s.timeout_s,
                readonly=self.read_only,
                account=self.account_id,
            )
        except Exception as exc:  # ib_async raises several exception types
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            raise BrokerError(
                f"cannot connect to IB at {s.host}:{s.port} (client {s.client_id}): {exc}"
            ) from exc

        accounts = list(self.ib.managedAccounts())
        self.health.managed_accounts = accounts
        if self.account_id not in accounts:
            self._closing = True
            self.ib.disconnect()
            raise AccountMismatch(
                f"account {self.account_id} is not managed by this login "
                f"(IB reports: {', '.join(accounts) or 'none'})"
            )

        # Ask for live data only. Delayed quotes are detected per ticker and refused.
        self.ib.reqMarketDataType(MARKET_DATA_LIVE)
        # Real-time daily P&L (about 1 s updates) for the margin estimate (T16).
        self._pnl = self.ib.reqPnL(self.account_id)
        self.health.connected = True
        self.health.last_connected_at = self._clock()
        self.health.reconnect_attempts = 0
        self.health.last_error = ""
        log.info(
            "connected to IB %s:%s account=%s read_only=%s live=%s",
            s.host,
            s.port,
            self.account_id,
            self.read_only,
            self.is_live,
        )
        self._publish(EVENT_CONNECTION, True)

    async def disconnect(self) -> None:
        self._closing = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()
        else:
            self._mark_disconnected()

    @property
    def is_connected(self) -> bool:
        return self.health.connected and self.ib.isConnected()

    def _mark_disconnected(self) -> None:
        was_connected = self.health.connected
        self.health.connected = False
        self.health.last_disconnected_at = self._clock()
        if was_connected:
            self._publish(EVENT_CONNECTION, False)

    def _on_disconnected(self) -> None:
        self._mark_disconnected()
        if self._closing:
            return
        log.warning("IB connection lost; reconnecting")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. interpreter shutdown); nothing to schedule
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = self.settings.reconnect_min_s
        while not self._closing and not self.is_connected:
            await self._sleep(delay)
            if self._closing:
                return
            self.health.reconnect_attempts += 1
            try:
                await self._connect_once()
            except AccountMismatch:
                log.error("account mismatch on reconnect; giving up")
                return
            except BrokerError as exc:
                log.warning(
                    "reconnect attempt %d failed: %s",
                    self.health.reconnect_attempts,
                    exc,
                )
                delay = min(delay * 2, self.settings.reconnect_max_s)

    async def wait_reconnected(self) -> None:
        """Wait for a pending reconnect (used by tests and the supervisor)."""
        if self._reconnect_task is not None:
            await self._reconnect_task

    # -- account (T07) -----------------------------------------------------------

    def _account_values(self) -> dict:
        """Latest numeric value per tag, preferring the BASE-currency row."""
        # Multi-currency accounts report each tag per currency plus "BASE".
        # Prefer BASE, then USD, then anything else.
        values: dict = {}
        ranks: dict = {}
        for av in self.ib.accountValues(self.account_id):
            if (
                av.account != self.account_id
                or av.tag not in ACCOUNT_TAGS
                or av.modelCode
            ):
                continue
            rank = {"BASE": 0, "USD": 1}.get(av.currency, 2)
            if rank >= ranks.get(av.tag, 3):
                continue
            try:
                values[av.tag] = float(av.value)
            except (TypeError, ValueError):
                continue
            ranks[av.tag] = rank
        return values

    def _build_snapshot(self) -> AccountSnapshot:
        values = self._account_values()
        missing = [tag for tag in ACCOUNT_TAGS if tag not in values]
        if missing:
            raise AccountDataUnavailable(
                f"IB has not sent account values yet: missing {', '.join(missing)}"
            )
        fields = {ACCOUNT_TAGS[tag]: values[tag] for tag in ACCOUNT_TAGS}
        return AccountSnapshot(
            account_id=self.account_id,
            ts=self._account_updated_at or self._clock(),
            **fields,
        )

    async def account_snapshot(self) -> AccountSnapshot:
        if not self.is_connected:
            raise BrokerDisconnected("not connected to IB")
        return self._build_snapshot()

    def daily_pnl(self) -> Optional[float]:
        value = getattr(self._pnl, "dailyPnL", None)
        return float(value) if _finite(value) else None

    def account_age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since IB last updated an account value (None if never)."""
        if self._account_updated_at is None:
            return None
        return ((now or self._clock()) - self._account_updated_at).total_seconds()

    def account_is_stale(self, now: Optional[datetime] = None) -> bool:
        age = self.account_age_seconds(now)
        return age is None or age > ACCOUNT_STALE_AFTER_S

    def _on_account_value(self, value) -> None:
        if value.account != self.account_id or value.tag not in ACCOUNT_TAGS:
            return
        self._account_updated_at = self._clock()
        try:
            snapshot = self._build_snapshot()
        except AccountDataUnavailable:
            return  # first batch still arriving
        self._publish(EVENT_ACCOUNT, snapshot)

    # -- not yet implemented (later plan tasks) ---------------------------------

    # -- positions (T08) --------------------------------------------------------

    async def positions(self) -> List[Position]:
        """Current positions with IB's market prices.

        Uses IB's portfolio items (they include a market price, refreshed
        with the account values about every 3 minutes). Falls back to the
        plain position list, without prices, if portfolio data hasn't arrived.
        Instruments the engine doesn't trade are listed in
        ``unsupported_positions`` and left out.
        """
        if not self.is_connected:
            raise BrokerDisconnected("not connected to IB")
        items = [
            i
            for i in self.ib.portfolio(self.account_id)
            if i.account == self.account_id
        ]
        if items:
            rows = [
                (i.contract, i.position, i.averageCost, i.marketPrice) for i in items
            ]
        else:
            rows = [
                (p.contract, p.position, p.avgCost, None)
                for p in self.ib.positions(self.account_id)
                if p.account == self.account_id
            ]
        result: List[Position] = []
        unsupported: List[str] = []
        for ib_contract, quantity, avg_cost, market_price in rows:
            if not quantity:
                continue
            contract = contract_from_ib(ib_contract)
            if contract is None:
                unsupported.append(
                    f"{getattr(ib_contract, 'secType', '?')} "
                    f"{getattr(ib_contract, 'localSymbol', '') or ib_contract.symbol}"
                )
                continue
            # IB reports option average cost per contract (premium x multiplier).
            per_unit_cost = float(avg_cost) / contract.multiplier
            price = None
            if market_price is not None and market_price == market_price:  # not NaN
                price = float(market_price)
            result.append(Position(contract, float(quantity), per_unit_cost, price))
        self.unsupported_positions = unsupported
        return result

    # -- contracts, chains and quotes (T09) ------------------------------------

    def _require_connection(self) -> None:
        if not self.is_connected:
            raise BrokerDisconnected("not connected to IB")

    async def _qualify(self, key: str, ib_contract):
        cached = self._qualified.get(key)
        if cached is not None:
            return cached
        await self.pacer.acquire()
        [qualified] = await self.ib.qualifyContractsAsync(ib_contract)
        if qualified is None or not getattr(qualified, "conId", 0):
            raise BrokerError(f"IB does not recognize contract {key}")
        self._qualified[key] = qualified
        return qualified

    async def option_chain(self, underlying: str) -> List[OptionChain]:
        """Expiries and strikes per trading class (e.g. SPX and SPXW)."""
        self._require_connection()
        und = await self._qualify(
            f"UND:{underlying.upper()}", underlying_to_ib(underlying)
        )
        await self.pacer.acquire()
        raw = await self.ib.reqSecDefOptParamsAsync(
            und.symbol, "", und.secType, und.conId
        )
        chains = []
        for chain in raw:
            if chain.exchange != "SMART":
                continue  # IB repeats the chain per exchange; SMART is the union
            expiries = tuple(
                sorted(
                    date(int(e[:4]), int(e[4:6]), int(e[6:8]))
                    for e in chain.expirations
                )
            )
            chains.append(
                OptionChain(
                    underlying=underlying.upper(),
                    trading_class=chain.tradingClass,
                    multiplier=int(float(chain.multiplier or 100)),
                    expiries=expiries,
                    strikes=tuple(sorted(float(k) for k in chain.strikes)),
                )
            )
        return chains

    def _quote_from_ticker(self, contract: Contract, ticker) -> Quote:
        greeks = None
        underlying_price = None
        model = getattr(ticker, "modelGreeks", None)
        if model is not None and _finite(model.delta):
            greeks = Greeks(
                delta=model.delta,
                gamma=model.gamma or 0.0,
                theta=model.theta or 0.0,
                vega=model.vega or 0.0,
                iv=_num(model.impliedVol),
            )
            underlying_price = _num(model.undPrice)
        ts = ticker.time or self._clock()
        return Quote(
            contract=contract,
            bid=_num(ticker.bid),
            ask=_num(ticker.ask),
            ts=ts,
            last=_num(ticker.last),
            underlying_price=underlying_price,
            greeks=greeks,
            delayed=getattr(ticker, "marketDataType", MARKET_DATA_LIVE)
            in DELAYED_DATA_TYPES,
        )

    async def quotes(self, contracts: Iterable[Contract]) -> List[Quote]:
        """Snapshot quotes with model Greeks, in paced batches.

        Delayed data is returned with ``delayed=True`` (callers must not
        trade on it). Open interest is not part of snapshots; the scanner
        (plan T30) requests it with streaming generic tick 101.
        """
        self._require_connection()
        contracts = list(contracts)
        qualified = [await self._qualify(c.key, contract_to_ib(c)) for c in contracts]
        result: List[Quote] = []
        for start in range(0, len(contracts), self.quote_batch_size):
            batch = qualified[start : start + self.quote_batch_size]
            await self.pacer.acquire(len(batch))
            tickers = await self.ib.reqTickersAsync(*batch)
            by_con = {t.contract.conId: t for t in tickers}
            for contract, ib_contract in zip(contracts[start:], batch):
                ticker = by_con.get(ib_contract.conId)
                if ticker is None:
                    raise BrokerError(f"no market data returned for {contract.key}")
                result.append(self._quote_from_ticker(contract, ticker))
        return result

    async def quote(self, contract: Contract) -> Quote:
        [result] = await self.quotes([contract])
        return result

    # -- history (T10) -----------------------------------------------------------

    async def daily_history(
        self, symbol: str, kind: HistoryKind, days: int
    ) -> List[Bar]:
        """Daily bars (regular trading hours), oldest first.

        ``days`` is in calendar days. IB caps daily requests at a few years
        per call, which is plenty for IV rank (1 year) and the 200-day MA.
        """
        self._require_connection()
        und = await self._qualify(f"UND:{symbol.upper()}", underlying_to_ib(symbol))
        duration = f"{max(1, days)} D" if days <= 365 else f"{math.ceil(days / 365)} Y"
        await self.pacer.acquire()
        raw = await self.ib.reqHistoricalDataAsync(
            und,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow=kind.value,
            useRTH=True,
            formatDate=1,
        )
        bars = []
        for bar in raw or []:
            # Daily bars come back as dates; intraday sizes would be datetimes.
            day = bar.date.date() if isinstance(bar.date, datetime) else bar.date
            bars.append(
                Bar(
                    day=day,
                    open=float(bar.open),
                    high=float(bar.high),
                    low=float(bar.low),
                    close=float(bar.close),
                    volume=float(bar.volume)
                    if _finite(bar.volume) and bar.volume > 0
                    else 0.0,
                )
            )
        return bars

    # -- what-if margin (T18b) ---------------------------------------------------

    async def build_ib_order(self, order: OrderRequest, what_if: bool = False):
        """(ib contract, ib order) for a domain order. Used by what-if and, in T22, placing.

        * Single leg: the leg's action, price = ``abs(limit_price)``.
        * Combo / roll: a BAG of SMART legs, bought at the **net** limit
          (positive = debit, negative = credit), as IB expects for combos.
        """
        from ib_async import (
            Bag,
            ComboLeg,
            LimitOrder,
        )  # pylint: disable=import-outside-toplevel

        qualified = [
            await self._qualify(leg.contract.key, contract_to_ib(leg.contract))
            for leg in order.legs
        ]
        if len(order.legs) == 1:
            [leg] = order.legs
            ib_contract = qualified[0]
            ib_order = LimitOrder(
                leg.action.value, leg.quantity, round(abs(order.limit_price), 2)
            )
        else:
            quantities = {leg.quantity for leg in order.legs}
            if len(quantities) != 1:
                raise BrokerError("combos must have equal leg quantities")
            ib_contract = Bag(
                symbol=qualified[0].symbol,
                exchange="SMART",
                currency=qualified[0].currency or "USD",
                comboLegs=[
                    ComboLeg(
                        conId=q.conId,
                        ratio=1,
                        action=leg.action.value,
                        exchange="SMART",
                    )
                    for q, leg in zip(qualified, order.legs)
                ],
            )
            ib_order = LimitOrder("BUY", quantities.pop(), round(order.limit_price, 2))
        ib_order.orderRef = order.order_ref
        ib_order.tif = order.tif
        ib_order.account = self.account_id
        ib_order.whatIf = what_if
        return ib_contract, ib_order

    async def what_if(self, order: OrderRequest) -> MarginImpact:
        """Account margin after the order, from IB, without placing it.

        Allowed on read-only connections: IB evaluates what-if orders without
        executing them.
        """
        self._require_connection()
        ib_contract, ib_order = await self.build_ib_order(order, what_if=True)
        await self.pacer.acquire()
        state = await self.ib.whatIfOrderAsync(ib_contract, ib_order)
        if (
            state is None
            or _num_or_none(getattr(state, "equityWithLoanAfter", None)) is None
        ):
            warning = getattr(state, "warningText", "") if state is not None else ""
            raise BrokerError(
                f"IB returned no what-if result for {order.order_ref} {warning}".strip()
            )
        return MarginImpact(
            equity_with_loan_after=_ib_float(state.equityWithLoanAfter),
            init_margin_after=_ib_float(state.initMarginAfter),
            maint_margin_after=_ib_float(state.maintMarginAfter),
            commission=_num_or_none(state.commission),
        )

    async def place_order(self, order):
        self._check_can_trade()
        raise NotImplementedError("plan T22")

    async def cancel_order(self, order_ref):
        raise NotImplementedError("plan T22")

    async def open_orders(self):
        raise NotImplementedError("plan T22")

    async def executions(self, since=None):
        raise NotImplementedError("plan T22")
