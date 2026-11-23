"""Data models for positions."""
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class OptionType(Enum):
    CALL = "C"
    PUT = "P"


class PositionSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class TradeAction(Enum):
    OPEN = "O"
    CLOSE = "C"
    ASSIGNED = "A"
    EXPIRED = "Ep"


@dataclass
class Dividend:
    """Represents a dividend payment."""
    symbol: str
    payment_date: date
    amount: float
    description: str = ""
    currency: str = "USD"

    @property
    def is_payment_in_lieu(self) -> bool:
        """True if this is a payment in lieu of dividend (from short positions)."""
        return "Payment in Lieu" in self.description


@dataclass
class BondInterest:
    """Represents a bond coupon/interest payment."""
    symbol: str
    payment_date: date
    amount: float
    description: str = ""
    currency: str = "USD"
    bond_name: str = ""  # Full bond name like "Amazon.com Inc AMZN 4.95 12/05/44"


@dataclass
class CashTransaction:
    """Represents a deposit or withdrawal."""
    transaction_date: date
    amount: float  # Positive for deposits, negative for withdrawals
    description: str = ""
    currency: str = "USD"

    @property
    def is_deposit(self) -> bool:
        return self.amount > 0

    @property
    def is_withdrawal(self) -> bool:
        return self.amount < 0


@dataclass
class Trade:
    """Represents a single trade."""
    symbol: str
    trade_date: datetime
    quantity: int
    price: float
    proceeds: float
    commission: float
    realized_pnl: float
    asset_type: str = "Option"  # "Option" or "Stock"
    underlying: str = ""
    strike: float = 0.0
    expiration: Optional[date] = None
    option_type: Optional[OptionType] = None
    action: Optional[TradeAction] = None

    @property
    def is_option(self) -> bool:
        return self.asset_type == "Option"

    @property
    def is_opening(self) -> bool:
        """True if this is an opening trade (selling premium)."""
        return self.action == TradeAction.OPEN

    @property
    def is_closing(self) -> bool:
        """True if this is a closing trade."""
        return self.action in (TradeAction.CLOSE, TradeAction.ASSIGNED, TradeAction.EXPIRED)

    @property
    def premium_collected(self) -> float:
        """Premium collected on opening short trades."""
        if self.is_opening and self.quantity < 0:
            return abs(self.proceeds)
        return 0.0

    @property
    def month_key(self) -> str:
        """Return YYYY-MM format for grouping."""
        return self.trade_date.strftime("%Y-%m")


@dataclass
class Position:
    """Base class for all positions."""
    symbol: str
    quantity: int
    cost_price: float
    current_price: float
    currency: str = "USD"
    
    @property
    def cost_basis(self) -> float:
        return abs(self.quantity) * self.cost_price * self.multiplier
    
    @property
    def market_value(self) -> float:
        return self.quantity * self.current_price * self.multiplier
    
    @property
    def unrealized_pnl(self) -> float:
        if self.quantity < 0:  # Short position
            return (self.cost_price - self.current_price) * abs(self.quantity) * self.multiplier
        else:  # Long position
            return (self.current_price - self.cost_price) * self.quantity * self.multiplier
    
    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100
    
    @property
    def multiplier(self) -> int:
        return 1
    
    @property
    def side(self) -> PositionSide:
        return PositionSide.LONG if self.quantity > 0 else PositionSide.SHORT


@dataclass
class StockPosition(Position):
    """Stock position."""
    asset_type: str = "Stock"
    
    @property
    def multiplier(self) -> int:
        return 1


@dataclass
class OptionPosition(Position):
    """Option position with Greeks and additional metrics."""
    underlying: str = ""
    strike: float = 0.0
    expiration: Optional[date] = None
    option_type: Optional[OptionType] = None
    underlying_price: float = 0.0
    asset_type: str = "Option"
    
    # Greeks (to be calculated)
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    implied_volatility: float = 0.0
    
    # Margin (to be calculated)
    margin_requirement: float = 0.0
    
    # Risk flags
    flags: list = field(default_factory=list)
    
    @property
    def multiplier(self) -> int:
        return 100
    
    @property
    def days_to_expiration(self) -> int:
        if self.expiration is None:
            return 0
        today = date.today()
        return max(0, (self.expiration - today).days)
    
    @property
    def is_call(self) -> bool:
        return self.option_type == OptionType.CALL
    
    @property
    def is_put(self) -> bool:
        return self.option_type == OptionType.PUT
    
    @property
    def is_short(self) -> bool:
        return self.quantity < 0
    
    @property
    def is_itm(self) -> bool:
        """Check if option is in-the-money."""
        if self.underlying_price == 0:
            return False
        if self.is_call:
            return self.underlying_price > self.strike
        else:
            return self.underlying_price < self.strike
    
    @property
    def moneyness(self) -> float:
        """Calculate moneyness (underlying / strike)."""
        if self.strike == 0:
            return 0.0
        return self.underlying_price / self.strike
    
    @property
    def intrinsic_value(self) -> float:
        """Calculate intrinsic value per share."""
        if self.underlying_price == 0:
            return 0.0
        if self.is_call:
            return max(0, self.underlying_price - self.strike)
        else:
            return max(0, self.strike - self.underlying_price)
    
    @property
    def extrinsic_value(self) -> float:
        """Calculate extrinsic (time) value per share."""
        return max(0, self.current_price - self.intrinsic_value)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "symbol": self.symbol,
            "underlying": self.underlying,
            "strike": self.strike,
            "expiration": self.expiration.isoformat() if self.expiration else None,
            "option_type": self.option_type.value if self.option_type else None,
            "quantity": self.quantity,
            "cost_price": self.cost_price,
            "current_price": self.current_price,
            "underlying_price": self.underlying_price,
            "currency": self.currency,
            "delta": self.delta,
            "gamma": self.gamma,
            "theta": self.theta,
            "vega": self.vega,
            "implied_volatility": self.implied_volatility,
            "margin_requirement": self.margin_requirement,
            "days_to_expiration": self.days_to_expiration,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "market_value": self.market_value,
            "cost_basis": self.cost_basis,
            "is_itm": self.is_itm,
            "flags": self.flags,
            "side": self.side.value,
        }

