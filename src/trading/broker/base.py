"""Broker adapter interface (plan T05, spec §4.1).

The engine talks to a broker only through :class:`BrokerAdapter`. Two
implementations exist: ``IBAdapter`` (T06, Interactive Brokers) and
:class:`~src.trading.broker.fake.FakeAdapter` (tests and backtests). Both
must pass the same interface test suite.

The interface is ``async`` because ``ib_async`` is asyncio-based.
"""

from __future__ import annotations

import abc
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

from src.trading.domain import (
    AccountSnapshot,
    Contract,
    Fill,
    MarginImpact,
    OrderRequest,
    Position,
    Quote,
)


class BrokerError(Exception):
    """Base class for broker errors."""


class BrokerDisconnected(BrokerError):
    """The broker connection is down; the call was not performed."""


class ReadOnlyError(BrokerError):
    """An order was attempted on a read-only connection (spec §4.3)."""


class OrderRejected(BrokerError):
    """The broker rejected the order."""


class OrderStatus(str, Enum):
    WORKING = "working"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"

    @property
    def is_done(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class OrderState:
    order: OrderRequest
    status: OrderStatus
    filled: int = 0
    avg_price: Optional[float] = None
    reason: str = ""

    @property
    def remaining(self) -> int:
        return self.order.legs[0].quantity - self.filled


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    trading_class: str
    multiplier: int
    expiries: Tuple[date, ...]
    strikes: Tuple[float, ...]


# Event names published by adapters.
EVENT_FILL = "fill"  # payload: Fill
EVENT_ORDER = "order"  # payload: OrderState
EVENT_ACCOUNT = "account"  # payload: AccountSnapshot
EVENT_POSITION = "position"  # payload: Position
EVENT_CONNECTION = "connection"  # payload: bool (connected)

EVENTS = (EVENT_FILL, EVENT_ORDER, EVENT_ACCOUNT, EVENT_POSITION, EVENT_CONNECTION)

Callback = Callable[[object], None]


class BrokerAdapter(abc.ABC):
    """Everything the engine needs from a broker."""

    def __init__(self, account_id: str, read_only: bool = True):
        self.account_id = account_id
        # Read-only is the default: placing orders must be asked for explicitly.
        self.read_only = read_only
        self._subscribers: Dict[str, List[Callback]] = defaultdict(list)

    # -- events -------------------------------------------------------------

    def subscribe(self, event: str, callback: Callback) -> None:
        if event not in EVENTS:
            raise ValueError(f"unknown event {event!r}")
        self._subscribers[event].append(callback)

    def _publish(self, event: str, payload: object) -> None:
        for callback in list(self._subscribers[event]):
            callback(payload)

    def _check_can_trade(self) -> None:
        if self.read_only:
            raise ReadOnlyError("adapter is read-only; orders are disabled")

    # -- connection ---------------------------------------------------------

    @abc.abstractmethod
    async def connect(self) -> None:
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool:
        ...

    # -- account and market data ---------------------------------------------

    @abc.abstractmethod
    async def account_snapshot(self) -> AccountSnapshot:
        ...

    @abc.abstractmethod
    async def positions(self) -> List[Position]:
        ...

    @abc.abstractmethod
    async def quote(self, contract: Contract) -> Quote:
        ...

    @abc.abstractmethod
    async def option_chain(self, underlying: str) -> List[OptionChain]:
        ...

    # -- orders -------------------------------------------------------------

    @abc.abstractmethod
    async def what_if(self, order: OrderRequest) -> MarginImpact:
        """Margin after the order, without placing it. Allowed when read-only."""

    @abc.abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderState:
        """Place a limit order. Must call ``_check_can_trade()`` first."""

    @abc.abstractmethod
    async def cancel_order(self, order_ref: str) -> OrderState:
        ...

    @abc.abstractmethod
    async def open_orders(self) -> List[OrderState]:
        ...

    @abc.abstractmethod
    async def executions(self, since: Optional[datetime] = None) -> List[Fill]:
        ...
