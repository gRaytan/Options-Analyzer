"""Broker-independent domain types (plan T03).

These types are what the strategy engine, risk gate and journal work with.
Nothing here imports the IB library; the broker adapter translates IB
objects to and from these types (spec §4.2).

Every type is a frozen dataclass with ``to_dict`` / ``from_dict`` so it can
be journaled as JSON and read back unchanged.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple, Union

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class ExerciseStyle(str, Enum):
    AMERICAN = "american"  # can be assigned any day (stock and ETF options)
    EUROPEAN = "european"  # exercised only at expiry (XSP, SPX)


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Action.BUY else -1


class IntentKind(str, Enum):
    OPEN = "open"
    CLOSE = "close"
    ROLL = "roll"
    HEDGE = "hedge"


class HistoryKind(str, Enum):
    """What a daily history series measures (IB ``whatToShow``)."""

    PRICE = "TRADES"  # OHLC prices (indexes: index levels)
    IMPLIED_VOL = "OPTION_IMPLIED_VOLATILITY"  # 30-day ATM implied vol, annualized
    HISTORICAL_VOL = "HISTORICAL_VOLATILITY"  # 30-day realized vol, annualized


class HaltScope(str, Enum):
    OPENING = "opening"  # no new risk; guards and closes continue
    ALL = "all"  # nothing is sent


# --------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StockContract:
    symbol: str
    currency: str = "USD"

    @property
    def key(self) -> str:
        return self.symbol

    @property
    def multiplier(self) -> int:
        return 1


@dataclass(frozen=True)
class OptionContract:
    underlying: str
    expiry: date
    strike: float
    right: Right
    multiplier: int = 100
    style: ExerciseStyle = ExerciseStyle.AMERICAN
    cash_settled: bool = False
    trading_class: Optional[str] = None  # e.g. "SPXW" vs "SPX"
    currency: str = "USD"

    @property
    def key(self) -> str:
        """Stable identifier, e.g. ``XSP 2026-12-18 P 550``."""
        return (
            f"{self.trading_class or self.underlying} {self.expiry.isoformat()} "
            f"{self.right.value} {self.strike:g}"
        )

    @property
    def can_be_assigned_early(self) -> bool:
        return self.style is ExerciseStyle.AMERICAN

    def days_to_expiry(self, today: date) -> int:
        return (self.expiry - today).days

    def intrinsic(self, underlying_price: float) -> float:
        if self.right is Right.CALL:
            return max(0.0, underlying_price - self.strike)
        return max(0.0, self.strike - underlying_price)

    def is_itm(self, underlying_price: float) -> bool:
        return self.intrinsic(underlying_price) > 0


Contract = Union[StockContract, OptionContract]


# --------------------------------------------------------------------------
# Market and account data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: Optional[float] = None


@dataclass(frozen=True)
class Quote:
    contract: Contract
    bid: Optional[float]
    ask: Optional[float]
    ts: datetime
    last: Optional[float] = None
    underlying_price: Optional[float] = None
    greeks: Optional[Greeks] = None
    open_interest: Optional[int] = None
    delayed: bool = False  # True when the broker served delayed data

    @property
    def mid(self) -> Optional[float]:
        if self.bid is None or self.ask is None or self.bid < 0 or self.ask < self.bid:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Optional[float]:
        if self.mid is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Optional[float]:
        """Spread as a fraction of mid (None when there's no valid market)."""
        mid = self.mid
        if mid is None or mid == 0:
            return None
        return self.spread / mid

    def age_seconds(self, now: datetime) -> float:
        return (now - self.ts).total_seconds()

    def extrinsic(self) -> Optional[float]:
        """Time value of an option quote (mid minus intrinsic)."""
        if not isinstance(self.contract, OptionContract):
            return None
        if self.mid is None or self.underlying_price is None:
            return None
        return max(0.0, self.mid - self.contract.intrinsic(self.underlying_price))


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    ts: datetime
    net_liquidation: float
    excess_liquidity: float
    maint_margin: float
    init_margin: float
    available_funds: float
    cash: float = 0.0

    @property
    def cushion(self) -> float:
        """IB Cushion: excess liquidity / net liquidation (spec R9)."""
        if self.net_liquidation <= 0:
            return 0.0
        return self.excess_liquidity / self.net_liquidation


@dataclass(frozen=True)
class Position:
    contract: Contract
    quantity: float  # negative = short
    avg_cost: float  # per share / per option (not multiplied)
    market_price: Optional[float] = None  # per share / per option, when known

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


@dataclass(frozen=True)
class Bar:
    """One daily bar. For volatility series, values are annualized fractions."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class MarginImpact:
    """Result of an IB what-if order: account margin *after* the order."""

    equity_with_loan_after: float
    init_margin_after: float
    maint_margin_after: float
    commission: Optional[float] = None

    @property
    def excess_liquidity_after(self) -> float:
        return self.equity_with_loan_after - self.maint_margin_after


# --------------------------------------------------------------------------
# Intents, orders, fills, halts
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderLeg:
    contract: Contract
    action: Action
    quantity: int  # always positive; direction comes from action

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("leg quantity must be positive")


def make_intent_id(
    strategy: str, kind: IntentKind, position_key: str, day: date
) -> str:
    """Deterministic ID: the same decision on the same day yields the same ID.

    It is sent to IB as ``orderRef`` so a restart can match working orders
    to intents and never send the same decision twice (spec §7.2).
    """
    raw = f"{strategy}|{kind.value}|{position_key}|{day.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    strategy: str
    kind: IntentKind
    legs: Tuple[OrderLeg, ...]
    trigger: str  # e.g. "PROFIT_TARGET_REACHED", "MARGIN_BREACH"
    created_at: datetime
    risk_reducing: bool = False
    chain_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.legs:
            raise ValueError("an intent needs at least one leg")


@dataclass(frozen=True)
class OrderRequest:
    intent_id: str
    legs: Tuple[OrderLeg, ...]
    limit_price: float  # net price per unit; positive = debit, negative = credit
    order_ref: str
    tif: str = "DAY"


@dataclass(frozen=True)
class Fill:
    exec_id: str
    order_ref: str
    contract: Contract
    action: Action
    quantity: int
    price: float
    ts: datetime
    commission: float = 0.0


@dataclass(frozen=True)
class HaltState:
    active: bool
    scope: HaltScope = HaltScope.OPENING
    reason: str = ""
    set_at: Optional[datetime] = None
    set_by: str = ""


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------

_TYPES = {
    cls.__name__: cls
    for cls in (
        StockContract,
        OptionContract,
        Greeks,
        Quote,
        AccountSnapshot,
        Position,
        MarginImpact,
        Bar,
        OrderLeg,
        TradeIntent,
        OrderRequest,
        Fill,
        HaltState,
    )
}
_ENUMS = {
    cls.__name__: cls for cls in (Right, ExerciseStyle, Action, IntentKind, HaltScope)
}


def to_dict(obj: Any) -> Any:
    """Convert a domain object to JSON-compatible data, tagged with its type."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        data = {f.name: to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        data["__type__"] = type(obj).__name__
        return data
    if isinstance(obj, Enum):
        return {"__enum__": type(obj).__name__, "value": obj.value}
    if isinstance(obj, datetime):
        return {"__datetime__": obj.isoformat()}
    if isinstance(obj, date):
        return {"__date__": obj.isoformat()}
    if isinstance(obj, tuple):
        return {"__tuple__": [to_dict(v) for v in obj]}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): to_dict(v) for k, v in obj.items()}
    return obj


def from_dict(data: Any) -> Any:
    """Inverse of :func:`to_dict`."""
    if isinstance(data, dict):
        if "__type__" in data:
            cls = _TYPES[data["__type__"]]
            kwargs = {k: from_dict(v) for k, v in data.items() if k != "__type__"}
            return cls(**kwargs)
        if "__enum__" in data:
            return _ENUMS[data["__enum__"]](data["value"])
        if "__datetime__" in data:
            return datetime.fromisoformat(data["__datetime__"])
        if "__date__" in data:
            return date.fromisoformat(data["__date__"])
        if "__tuple__" in data:
            return tuple(from_dict(v) for v in data["__tuple__"])
        return {k: from_dict(v) for k, v in data.items()}
    if isinstance(data, list):
        return [from_dict(v) for v in data]
    return data


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
