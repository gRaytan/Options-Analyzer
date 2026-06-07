"""Margin calculation for option positions."""
from typing import Optional
from src.models.position import OptionPosition, OptionType
from src.models.portfolio import Portfolio


class MarginCalculator:
    """Calculate margin requirements for option positions."""
    
    def __init__(self, margin_type: str = "reg_t"):
        """
        Initialize margin calculator.
        
        Args:
            margin_type: "reg_t" for Regulation T or "portfolio" for Portfolio Margin
        """
        self.margin_type = margin_type
    
    def calculate_margin(self, option: OptionPosition,
                         naked_quantity: Optional[float] = None) -> float:
        """Calculate margin requirement for an option position.

        Args:
            option: The option position.
            naked_quantity: Number of contracts to treat as naked (uncovered).
                Defaults to the full absolute position size. Pass a reduced
                value when some contracts are covered by stock holdings
                (e.g. covered calls / covered puts require no extra margin).
                May be fractional when shares cover only part of a contract
                (e.g. 80 shares against 1 short call -> 0.2 naked contracts).
        """
        if not option.is_short:
            # Long options don't require margin (just the premium paid)
            return 0.0

        if naked_quantity is None:
            naked_quantity = abs(option.quantity)

        if naked_quantity <= 0:
            # Fully covered by stock -> no additional margin required.
            return 0.0

        if self.margin_type == "portfolio":
            return self._calculate_portfolio_margin(option, naked_quantity)
        else:
            return self._calculate_reg_t_margin(option, naked_quantity)

    def _calculate_reg_t_margin(self, option: OptionPosition, qty: int) -> float:
        """
        Calculate Reg-T margin for a short option.

        For naked short options (IBKR/CBOE Reg-T baseline):
        - Short Call: Max(20% of underlying - OTM amount, 10% of underlying) + premium
        - Short Put: Max(20% of underlying - OTM amount, 10% of strike) + premium
        """
        S = option.underlying_price
        K = option.strike
        multiplier = option.multiplier
        premium = option.current_price * multiplier * qty

        if S <= 0:
            # Can't calculate without underlying price
            return premium
        
        if option.is_call:
            # Short call margin: premium + max(20% underlying - OTM, 10% underlying)
            otm_amount = max(0, K - S) * multiplier * qty
            margin_20 = 0.20 * S * multiplier * qty - otm_amount
            margin_10 = 0.10 * S * multiplier * qty
            margin = max(margin_20, margin_10) + premium
        else:
            # Short put margin: premium + max(20% underlying - OTM, 10% strike)
            otm_amount = max(0, S - K) * multiplier * qty
            margin_20 = 0.20 * S * multiplier * qty - otm_amount
            margin_10 = 0.10 * K * multiplier * qty
            margin = max(margin_20, margin_10) + premium

        return margin
    
    def _calculate_portfolio_margin(self, option: OptionPosition, qty: int) -> float:
        """
        Calculate Portfolio Margin for a short option.

        Portfolio margin is typically based on stress testing and is usually
        lower than Reg-T. We use a simplified model here:
        - Approximately 15% of notional for ATM options
        - Reduced for OTM options based on delta
        """
        S = option.underlying_price
        K = option.strike
        multiplier = option.multiplier
        premium = option.current_price * multiplier * qty

        if S <= 0:
            return premium
        
        # Base margin is 15% of notional
        notional = S * multiplier * qty
        base_margin = 0.15 * notional
        
        # Adjust based on moneyness
        if option.is_call:
            moneyness = S / K  # > 1 means ITM for calls
        else:
            moneyness = K / S  # > 1 means ITM for puts
        
        # Scale margin based on moneyness
        if moneyness < 0.8:
            # Deep OTM - reduce margin
            margin_factor = 0.5
        elif moneyness < 0.95:
            # OTM - slightly reduced
            margin_factor = 0.75
        elif moneyness <= 1.05:
            # ATM - full margin
            margin_factor = 1.0
        else:
            # ITM - increased margin
            margin_factor = 1.0 + (moneyness - 1.05) * 2
        
        margin = base_margin * margin_factor + premium
        
        return margin


SHARES_PER_CONTRACT = 100


def calculate_portfolio_margin(portfolio: Portfolio,
                                margin_type: str = "reg_t") -> float:
    """
    Calculate and update margin requirements for all options in a portfolio.

    Stock holdings are used to offset short options:
    - Long shares cover short calls (a covered call needs no extra margin).
    - Short shares cover short puts (a covered put needs no extra margin).

    Each short option contract is covered by SHARES_PER_CONTRACT (100) shares
    of the matching underlying. Shares are credited proportionally, so a
    partial holding (e.g. 80 shares against 1 short call) covers part of the
    contract and only the uncovered fraction is margined as naked.

    Returns:
        Total margin requirement for the portfolio
    """
    calculator = MarginCalculator(margin_type)

    # Pools of shares available to cover short options, keyed by underlying.
    long_shares: dict = {}
    short_shares: dict = {}
    for stock in portfolio.stocks:
        if stock.quantity > 0:
            long_shares[stock.symbol] = long_shares.get(stock.symbol, 0) + stock.quantity
        elif stock.quantity < 0:
            short_shares[stock.symbol] = short_shares.get(stock.symbol, 0) + abs(stock.quantity)

    total_margin = 0.0

    for option in portfolio.options:
        if not option.is_short:
            option.margin_requirement = 0.0
            continue

        contracts = abs(option.quantity)
        underlying = option.underlying

        # Long shares cover short calls; short shares cover short puts.
        pool = long_shares if option.is_call else short_shares
        available = pool.get(underlying, 0)

        shares_needed = contracts * SHARES_PER_CONTRACT
        covered_shares = min(shares_needed, available)
        if covered_shares > 0:
            pool[underlying] = available - covered_shares

        # Credit shares proportionally; a partial holding covers part of a
        # contract, leaving only the uncovered fraction to be margined naked.
        naked_contracts = (shares_needed - covered_shares) / SHARES_PER_CONTRACT

        margin = calculator.calculate_margin(option, naked_quantity=naked_contracts)
        option.margin_requirement = margin
        total_margin += margin

    return total_margin

