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
    
    def calculate_margin(self, option: OptionPosition) -> float:
        """Calculate margin requirement for an option position."""
        if not option.is_short:
            # Long options don't require margin (just the premium paid)
            return 0.0
        
        if self.margin_type == "portfolio":
            return self._calculate_portfolio_margin(option)
        else:
            return self._calculate_reg_t_margin(option)
    
    def _calculate_reg_t_margin(self, option: OptionPosition) -> float:
        """
        Calculate Reg-T margin for a short option.
        
        For naked short options:
        - Short Call: Max(25% of underlying - OTM amount, 10% of underlying) + premium
        - Short Put: Max(25% of underlying - OTM amount, 10% of strike) + premium
        """
        S = option.underlying_price
        K = option.strike
        premium = option.current_price * option.multiplier * abs(option.quantity)
        multiplier = option.multiplier
        qty = abs(option.quantity)
        
        if S <= 0:
            # Can't calculate without underlying price
            return premium
        
        if option.is_call:
            # Short call margin
            otm_amount = max(0, K - S) * multiplier * qty
            margin_25 = 0.25 * S * multiplier * qty - otm_amount
            margin_10 = 0.10 * S * multiplier * qty
            margin = max(margin_25, margin_10) + premium
        else:
            # Short put margin
            otm_amount = max(0, S - K) * multiplier * qty
            margin_25 = 0.25 * S * multiplier * qty - otm_amount
            margin_10 = 0.10 * K * multiplier * qty
            margin = max(margin_25, margin_10) + premium
        
        return margin
    
    def _calculate_portfolio_margin(self, option: OptionPosition) -> float:
        """
        Calculate Portfolio Margin for a short option.
        
        Portfolio margin is typically based on stress testing and is usually
        lower than Reg-T. We use a simplified model here:
        - Approximately 15% of notional for ATM options
        - Reduced for OTM options based on delta
        """
        S = option.underlying_price
        K = option.strike
        premium = option.current_price * option.multiplier * abs(option.quantity)
        multiplier = option.multiplier
        qty = abs(option.quantity)
        
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


def calculate_portfolio_margin(portfolio: Portfolio, 
                                margin_type: str = "reg_t") -> float:
    """
    Calculate and update margin requirements for all options in a portfolio.
    
    Returns:
        Total margin requirement for the portfolio
    """
    calculator = MarginCalculator(margin_type)
    total_margin = 0.0
    
    for option in portfolio.options:
        margin = calculator.calculate_margin(option)
        option.margin_requirement = margin
        total_margin += margin
    
    return total_margin

