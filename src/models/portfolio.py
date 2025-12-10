"""Portfolio model for aggregating positions."""
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from collections import defaultdict
from .position import OptionPosition, StockPosition, Trade, Dividend


@dataclass
class PortfolioGreeks:
    """Aggregated portfolio Greeks."""
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass
class Portfolio:
    """Portfolio containing all positions and summary metrics."""
    options: List[OptionPosition] = field(default_factory=list)
    stocks: List[StockPosition] = field(default_factory=list)
    trades: List[Trade] = field(default_factory=list)
    dividends: List[Dividend] = field(default_factory=list)

    # Account info
    account_id: str = ""
    account_name: str = ""
    base_currency: str = "USD"
    statement_period: str = ""

    # NAV data
    net_asset_value: float = 0.0
    cash_balance: float = 0.0
    
    @property
    def total_positions(self) -> int:
        return len(self.options) + len(self.stocks)
    
    @property
    def total_option_positions(self) -> int:
        return len(self.options)
    
    @property
    def total_stock_positions(self) -> int:
        return len(self.stocks)
    
    @property
    def options_market_value(self) -> float:
        return sum(opt.market_value for opt in self.options)
    
    @property
    def stocks_market_value(self) -> float:
        return sum(stock.market_value for stock in self.stocks)
    
    @property
    def total_market_value(self) -> float:
        return self.options_market_value + self.stocks_market_value
    
    @property
    def options_unrealized_pnl(self) -> float:
        return sum(opt.unrealized_pnl for opt in self.options)
    
    @property
    def stocks_unrealized_pnl(self) -> float:
        return sum(stock.unrealized_pnl for stock in self.stocks)
    
    @property
    def total_unrealized_pnl(self) -> float:
        return self.options_unrealized_pnl + self.stocks_unrealized_pnl
    
    @property
    def total_margin_requirement(self) -> float:
        return sum(opt.margin_requirement for opt in self.options)
    
    @property
    def portfolio_greeks(self) -> PortfolioGreeks:
        """Calculate aggregated portfolio Greeks."""
        return PortfolioGreeks(
            delta=sum(opt.delta * opt.quantity for opt in self.options),
            gamma=sum(opt.gamma * opt.quantity for opt in self.options),
            theta=sum(opt.theta * opt.quantity for opt in self.options),
            vega=sum(opt.vega * opt.quantity for opt in self.options),
        )
    
    @property
    def short_options(self) -> List[OptionPosition]:
        """Get all short option positions."""
        return [opt for opt in self.options if opt.is_short]
    
    @property
    def long_options(self) -> List[OptionPosition]:
        """Get all long option positions."""
        return [opt for opt in self.options if not opt.is_short]
    
    @property
    def short_calls(self) -> List[OptionPosition]:
        """Get all short call positions."""
        return [opt for opt in self.options if opt.is_short and opt.is_call]
    
    @property
    def short_puts(self) -> List[OptionPosition]:
        """Get all short put positions."""
        return [opt for opt in self.options if opt.is_short and opt.is_put]
    
    @property
    def flagged_positions(self) -> List[OptionPosition]:
        """Get all positions with risk flags."""
        return [opt for opt in self.options if opt.flags]
    
    def get_underlying_price(self, symbol: str) -> Optional[float]:
        """Get underlying price from stock positions if available."""
        for stock in self.stocks:
            if stock.symbol == symbol:
                return stock.current_price
        return None
    
    def get_options_by_underlying(self, underlying: str) -> List[OptionPosition]:
        """Get all options for a specific underlying."""
        return [opt for opt in self.options if opt.underlying == underlying]
    
    def get_options_expiring_soon(self, days: int = 7) -> List[OptionPosition]:
        """Get options expiring within specified days."""
        return [opt for opt in self.options if opt.days_to_expiration <= days]

    def merge(self, other: 'Portfolio') -> None:
        """Merge another portfolio into this one.

        - Positions are replaced (uses latest file's positions)
        - Trades are combined and deduplicated
        - Account info uses the first non-empty value
        """
        # For positions, use the other portfolio's positions (latest upload)
        # This assumes the latest file has the current positions
        if other.options:
            self.options = other.options
        if other.stocks:
            self.stocks = other.stocks

        # Merge trades, deduplicating by (symbol, date, quantity, price)
        existing_trades = {
            (t.symbol, t.trade_date, t.quantity, t.price): t
            for t in self.trades
        }
        for trade in other.trades:
            key = (trade.symbol, trade.trade_date, trade.quantity, trade.price)
            if key not in existing_trades:
                self.trades.append(trade)
                existing_trades[key] = trade

        # Use first non-empty account info
        if not self.account_id and other.account_id:
            self.account_id = other.account_id
        if not self.account_name and other.account_name:
            self.account_name = other.account_name

        # Update NAV from latest
        if other.net_asset_value:
            self.net_asset_value = other.net_asset_value
        if other.cash_balance:
            self.cash_balance = other.cash_balance

        # Combine statement periods
        if other.statement_period:
            if self.statement_period:
                self.statement_period = f"{self.statement_period}, {other.statement_period}"
            else:
                self.statement_period = other.statement_period

    @property
    def option_trades(self) -> List[Trade]:
        """Get all option trades."""
        return [t for t in self.trades if t.is_option]

    def get_monthly_realized_pnl(self) -> Dict[str, float]:
        """Get realized P&L by month for options."""
        monthly = defaultdict(float)
        for trade in self.option_trades:
            if trade.realized_pnl != 0:
                monthly[trade.month_key] += trade.realized_pnl
        return dict(sorted(monthly.items()))

    def get_monthly_premium_collected(self) -> Dict[str, float]:
        """Get NET premium by month (opens - closes).

        This calculates the true net cash flow from options trading:
        - Opens (selling): we receive premium (positive)
        - Closes (buying back): we pay premium (negative)
        - Net = received - paid

        Can be negative if you paid more to close positions than you received
        from opening new ones (e.g., during a market downturn when closing losers).
        """
        monthly_opens = defaultdict(float)
        monthly_closes = defaultdict(float)

        for trade in self.option_trades:
            month = trade.month_key
            if trade.is_opening and trade.quantity < 0:
                # Selling to open - we receive premium
                monthly_opens[month] += abs(trade.proceeds)
            elif trade.is_closing and trade.quantity > 0:
                # Buying to close - we pay premium
                monthly_closes[month] += abs(trade.proceeds)

        # Calculate net for each month
        all_months = set(monthly_opens.keys()) | set(monthly_closes.keys())
        monthly_net = {
            month: monthly_opens[month] - monthly_closes[month]
            for month in all_months
        }

        return dict(sorted(monthly_net.items()))

    def get_monthly_trades_count(self) -> Dict[str, int]:
        """Get number of trades by month."""
        monthly = defaultdict(int)
        for trade in self.option_trades:
            monthly[trade.month_key] += 1
        return dict(sorted(monthly.items()))

    @property
    def total_realized_pnl(self) -> float:
        """Total realized P&L from all option trades."""
        return sum(t.realized_pnl for t in self.option_trades)

    @property
    def total_premium_collected(self) -> float:
        """Total NET premium collected, accounting for rolls."""
        monthly = self.get_monthly_premium_collected()
        return sum(monthly.values())

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        greeks = self.portfolio_greeks
        return {
            "account_id": self.account_id,
            "account_name": self.account_name,
            "base_currency": self.base_currency,
            "statement_period": self.statement_period,
            "net_asset_value": self.net_asset_value,
            "cash_balance": self.cash_balance,
            "total_positions": self.total_positions,
            "total_option_positions": self.total_option_positions,
            "total_stock_positions": self.total_stock_positions,
            "options_market_value": self.options_market_value,
            "stocks_market_value": self.stocks_market_value,
            "total_market_value": self.total_market_value,
            "options_unrealized_pnl": self.options_unrealized_pnl,
            "stocks_unrealized_pnl": self.stocks_unrealized_pnl,
            "total_unrealized_pnl": self.total_unrealized_pnl,
            "total_margin_requirement": self.total_margin_requirement,
            "portfolio_greeks": {
                "delta": greeks.delta,
                "gamma": greeks.gamma,
                "theta": greeks.theta,
                "vega": greeks.vega,
            },
            "short_options_count": len(self.short_options),
            "flagged_positions_count": len(self.flagged_positions),
            "options": [opt.to_dict() for opt in self.options],
            "stocks": [{"symbol": s.symbol, "quantity": s.quantity, 
                       "current_price": s.current_price, "unrealized_pnl": s.unrealized_pnl}
                      for s in self.stocks],
        }

