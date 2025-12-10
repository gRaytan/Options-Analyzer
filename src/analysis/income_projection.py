"""Income projection calculations for dividends and bond interest."""
import yfinance as yf
from typing import Dict, List, Optional
from dataclasses import dataclass
import re


@dataclass
class DividendProjection:
    """Projected dividend info for a stock."""
    symbol: str
    shares: float
    current_price: float
    forward_dividend_rate: float  # Annual dividend per share
    projected_annual: float  # Total projected annual dividend
    dividend_yield: float  # Yield percentage
    cagr_5yr: Optional[float]  # 5-year dividend growth rate
    projected_with_growth: float  # Projected with CAGR applied


@dataclass
class BondProjection:
    """Projected interest for a bond."""
    symbol: str
    bond_name: str
    coupon_rate: float  # Annual coupon rate (e.g., 4.95%)
    annual_interest: float  # Projected annual interest


@dataclass
class IncomeProjection:
    """Complete income projection."""
    stock_projections: List[DividendProjection]
    bond_projections: List[BondProjection]
    total_dividend_projection: float
    total_dividend_with_growth: float
    total_bond_projection: float
    total_income_projection: float
    total_income_with_growth: float
    avg_dividend_cagr: float


def calculate_dividend_cagr(symbol: str, years: int = 5) -> Optional[float]:
    """Calculate dividend CAGR for a stock over specified years."""
    try:
        ticker = yf.Ticker(symbol)
        divs = ticker.dividends
        
        if len(divs) < 4:  # Need at least a couple years of data
            return None
        
        # Get annual dividends
        annual = divs.resample('YE').sum()
        
        if len(annual) < 2:
            return None
        
        # Get last N+1 years for N-year CAGR
        recent = annual.tail(min(years + 1, len(annual)))
        
        if len(recent) < 2:
            return None
        
        start_val = recent.iloc[0]
        end_val = recent.iloc[-1]
        n_years = len(recent) - 1
        
        if start_val <= 0 or end_val <= 0:
            return None
        
        cagr = (end_val / start_val) ** (1 / n_years) - 1
        return cagr
    except Exception:
        return None


def get_forward_dividend_rate(symbol: str) -> Optional[float]:
    """Get forward annual dividend rate from yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        
        # Try forward dividend rate first
        rate = info.get('dividendRate')
        if rate and rate > 0:
            return rate
        
        # Fall back to trailing
        rate = info.get('trailingAnnualDividendRate')
        if rate and rate > 0:
            return rate
        
        return None
    except Exception:
        return None


def parse_bond_coupon_rate(bond_name: str) -> Optional[float]:
    """Extract coupon rate from bond name like 'AMZN 4.95 12/05/44'."""
    # Pattern: symbol followed by rate (e.g., "AMZN 4.95" or "4.95")
    match = re.search(r'(\d+\.?\d*)\s+\d{2}/\d{2}/\d{2}', bond_name)
    if match:
        return float(match.group(1))
    return None


def calculate_income_projection(
    dividend_stocks: List[Dict],
    bond_list: List[Dict],
    months_of_data: int = 12
) -> IncomeProjection:
    """Calculate projected income for next year.
    
    Args:
        dividend_stocks: List of dicts with symbol, shares, current_price, ytd_dividends
        bond_list: List of dicts with symbol, bond_name, ytd_interest, payment_count
        months_of_data: Number of months of YTD data available
    
    Returns:
        IncomeProjection with all projections
    """
    stock_projections = []
    total_dividend = 0.0
    total_dividend_with_growth = 0.0
    cagr_values = []
    
    for stock in dividend_stocks:
        symbol = stock['symbol']
        shares = stock['shares']
        current_price = stock.get('current_price', 0)
        
        # Get forward dividend rate from yfinance
        forward_rate = get_forward_dividend_rate(symbol)
        
        if forward_rate is None:
            # Fall back to annualizing YTD
            ytd = stock.get('ytd_dividends', 0)
            if months_of_data > 0 and ytd > 0:
                forward_rate = (ytd / shares / months_of_data) * 12 if shares > 0 else 0
            else:
                forward_rate = 0
        
        projected_annual = forward_rate * shares
        dividend_yield = (forward_rate / current_price * 100) if current_price > 0 else 0
        
        # Get CAGR
        cagr = calculate_dividend_cagr(symbol)
        
        # Project with growth (apply CAGR for next year)
        if cagr is not None and cagr > -0.5:  # Ignore extreme negative CAGR
            projected_with_growth = projected_annual * (1 + cagr)
            cagr_values.append(cagr)
        else:
            projected_with_growth = projected_annual
        
        stock_projections.append(DividendProjection(
            symbol=symbol,
            shares=shares,
            current_price=current_price,
            forward_dividend_rate=forward_rate,
            projected_annual=projected_annual,
            dividend_yield=dividend_yield,
            cagr_5yr=cagr,
            projected_with_growth=projected_with_growth,
        ))
        
        total_dividend += projected_annual
        total_dividend_with_growth += projected_with_growth
    
    # Bond projections
    bond_projections = []
    total_bond = 0.0
    
    for bond in bond_list:
        symbol = bond['symbol']
        bond_name = bond.get('bond_name', '')
        ytd_interest = bond.get('ytd_interest', 0)
        payment_count = bond.get('payment_count', 0)
        
        # Try to get coupon rate from bond name
        coupon_rate = parse_bond_coupon_rate(bond_name)
        
        # Estimate annual interest
        # Bonds typically pay semi-annually
        if payment_count > 0:
            per_payment = ytd_interest / payment_count
            annual_interest = per_payment * 2  # Semi-annual assumption
        else:
            annual_interest = ytd_interest
        
        bond_projections.append(BondProjection(
            symbol=symbol,
            bond_name=bond_name,
            coupon_rate=coupon_rate or 0,
            annual_interest=annual_interest,
        ))
        
        total_bond += annual_interest
    
    # Calculate average CAGR
    avg_cagr = sum(cagr_values) / len(cagr_values) if cagr_values else 0
    
    return IncomeProjection(
        stock_projections=stock_projections,
        bond_projections=bond_projections,
        total_dividend_projection=total_dividend,
        total_dividend_with_growth=total_dividend_with_growth,
        total_bond_projection=total_bond,
        total_income_projection=total_dividend + total_bond,
        total_income_with_growth=total_dividend_with_growth + total_bond,
        avg_dividend_cagr=avg_cagr,
    )

