"""Income projection calculations for dividends and bond interest."""
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import re

# Global cache for dividend data
_dividend_cache: Dict[str, Dict[str, Any]] = {}
_dividend_cache_lock = threading.Lock()
_last_yf_request = 0
_yf_request_lock = threading.Lock()

DIVIDEND_CACHE_EXPIRY = 3600  # 1 hour
MIN_YF_INTERVAL = 0.5  # 500ms between requests


def _rate_limit_yf():
    """Rate limit yfinance requests."""
    global _last_yf_request
    with _yf_request_lock:
        now = time.time()
        elapsed = now - _last_yf_request
        if elapsed < MIN_YF_INTERVAL:
            time.sleep(MIN_YF_INTERVAL - elapsed)
        _last_yf_request = time.time()


def _get_yf_ticker(symbol: str):
    """Get yfinance ticker with rate limiting."""
    import yfinance as yf

    _rate_limit_yf()
    return yf.Ticker(symbol)


def _get_cached_dividend_data(symbol: str, key: str) -> Optional[Any]:
    """Get cached dividend data if not expired."""
    with _dividend_cache_lock:
        if symbol in _dividend_cache:
            entry = _dividend_cache[symbol]
            if datetime.now() - entry.get("timestamp", datetime.min) < timedelta(
                seconds=DIVIDEND_CACHE_EXPIRY
            ):
                return entry.get(key)
    return None


def _set_cached_dividend_data(symbol: str, key: str, value: Any):
    """Set cached dividend data."""
    with _dividend_cache_lock:
        if symbol not in _dividend_cache:
            _dividend_cache[symbol] = {"timestamp": datetime.now()}
        _dividend_cache[symbol][key] = value
        _dividend_cache[symbol]["timestamp"] = datetime.now()


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
    frequency: str  # 'monthly', 'quarterly', 'semi-annual', 'annual'
    payment_months: List[int]  # Months when dividends are paid (1-12)


@dataclass
class BondProjection:
    """Projected interest for a bond."""

    symbol: str
    bond_name: str
    coupon_rate: float  # Annual coupon rate (e.g., 4.95%)
    annual_interest: float  # Projected annual interest
    payment_months: List[int]  # Months when interest is paid (1-12)


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
    monthly_breakdown: Dict[
        int, Dict[str, float]
    ]  # {month: {dividends, bonds, growth_bonus}}


def get_dividend_schedule(symbol: str) -> tuple[str, List[int]]:
    """Detect dividend frequency and payment months from historical data.

    Returns:
        Tuple of (frequency, payment_months) where frequency is one of:
        'monthly', 'quarterly', 'semi-annual', 'annual', 'unknown'
        and payment_months is a list of months (1-12) when dividends are typically paid.
    """
    # Check cache first
    cached = _get_cached_dividend_data(symbol, "schedule")
    if cached is not None:
        return cached

    try:
        ticker = _get_yf_ticker(symbol)
        divs = ticker.dividends

        if len(divs) < 2:
            result = ("unknown", [3, 6, 9, 12])
            _set_cached_dividend_data(symbol, "schedule", result)
            return result

        # Get dividends from last 2 years
        two_years_ago = datetime.now() - timedelta(days=730)
        recent_divs = divs[divs.index >= two_years_ago.strftime("%Y-%m-%d")]

        if len(recent_divs) < 2:
            recent_divs = divs.tail(8)  # Fall back to last 8 dividends

        # Extract months of payments
        payment_months = sorted(set(d.month for d in recent_divs.index))
        payments_per_year = len(recent_divs) / 2  # Average over 2 years

        # Determine frequency
        if payments_per_year >= 10:
            frequency = "monthly"
            payment_months = list(range(1, 13))
        elif payments_per_year >= 3:
            frequency = "quarterly"
            if len(payment_months) < 4:
                payment_months = [3, 6, 9, 12]
        elif payments_per_year >= 1.5:
            frequency = "semi-annual"
            if len(payment_months) < 2:
                payment_months = [6, 12]
        elif payments_per_year >= 0.5:
            frequency = "annual"
            if len(payment_months) < 1:
                payment_months = [12]
        else:
            frequency = "unknown"
            payment_months = [3, 6, 9, 12]

        result = (frequency, payment_months)
        _set_cached_dividend_data(symbol, "schedule", result)
        return result
    except Exception as e:
        if "429" in str(e):
            time.sleep(2)  # Rate limited, wait
        result = ("quarterly", [3, 6, 9, 12])
        _set_cached_dividend_data(symbol, "schedule", result)
        return result


def calculate_dividend_cagr(symbol: str, years: int = 5) -> Optional[float]:
    """Calculate dividend CAGR for a stock over specified years."""
    # Check cache first
    cached = _get_cached_dividend_data(symbol, "cagr")
    if cached is not None:
        return cached

    try:
        ticker = _get_yf_ticker(symbol)
        divs = ticker.dividends

        if len(divs) < 4:
            _set_cached_dividend_data(symbol, "cagr", None)
            return None

        # Get annual dividends - try 'YE' first (newer pandas), fall back to 'Y'
        try:
            annual = divs.resample("YE").sum()
        except ValueError:
            annual = divs.resample("Y").sum()

        if len(annual) < 2:
            _set_cached_dividend_data(symbol, "cagr", None)
            return None

        # Get last N+1 years for N-year CAGR
        recent = annual.tail(min(years + 1, len(annual)))

        if len(recent) < 2:
            _set_cached_dividend_data(symbol, "cagr", None)
            return None

        start_val = recent.iloc[0]
        end_val = recent.iloc[-1]
        n_years = len(recent) - 1

        if start_val <= 0 or end_val <= 0:
            _set_cached_dividend_data(symbol, "cagr", None)
            return None

        cagr = (end_val / start_val) ** (1 / n_years) - 1
        _set_cached_dividend_data(symbol, "cagr", cagr)
        return cagr
    except Exception as e:
        if "429" in str(e):
            time.sleep(2)
        _set_cached_dividend_data(symbol, "cagr", None)
        return None


def get_forward_dividend_rate(symbol: str) -> Optional[float]:
    """Get forward annual dividend rate from yfinance."""
    # Check cache first
    cached = _get_cached_dividend_data(symbol, "forward_rate")
    if cached is not None:
        return cached if cached > 0 else None

    try:
        ticker = _get_yf_ticker(symbol)
        info = ticker.info

        # Try forward dividend rate first
        rate = info.get("dividendRate")
        if rate and rate > 0:
            _set_cached_dividend_data(symbol, "forward_rate", rate)
            return rate

        # Fall back to trailing
        rate = info.get("trailingAnnualDividendRate")
        if rate and rate > 0:
            _set_cached_dividend_data(symbol, "forward_rate", rate)
            return rate

        _set_cached_dividend_data(symbol, "forward_rate", 0)
        return None
    except Exception as e:
        if "429" in str(e):
            time.sleep(2)
        _set_cached_dividend_data(symbol, "forward_rate", 0)
        return None


def parse_bond_coupon_rate(bond_name: str) -> Optional[float]:
    """Extract coupon rate from bond name like 'AMZN 4.95 12/05/44'."""
    # Pattern: symbol followed by rate (e.g., "AMZN 4.95" or "4.95")
    match = re.search(r"(\d+\.?\d*)\s+\d{2}/\d{2}/\d{2}", bond_name)
    if match:
        return float(match.group(1))
    return None


def get_bond_payment_months(bond_name: str) -> List[int]:
    """Estimate bond payment months from maturity date.

    Bonds typically pay semi-annually, 6 months apart.
    We use the maturity month to estimate payment schedule.
    """
    # Try to extract maturity date from bond name like "AMZN 4.95 12/05/44"
    match = re.search(r"(\d{2})/\d{2}/\d{2}", bond_name)
    if match:
        maturity_month = int(match.group(1))
        # Semi-annual payments: maturity month and 6 months before
        other_month = (
            (maturity_month - 6) if maturity_month > 6 else (maturity_month + 6)
        )
        return sorted([maturity_month, other_month])
    # Default to June and December
    return [6, 12]


def calculate_income_projection(
    dividend_stocks: List[Dict], bond_list: List[Dict], months_of_data: int = 12
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

    # Initialize monthly breakdown
    monthly_breakdown = {
        m: {"dividends": 0.0, "bonds": 0.0, "growth_bonus": 0.0} for m in range(1, 13)
    }

    for stock in dividend_stocks:
        symbol = stock["symbol"]
        shares = stock["shares"]
        current_price = stock.get("current_price", 0)

        # Get forward dividend rate from yfinance
        forward_rate = get_forward_dividend_rate(symbol)

        if forward_rate is None:
            # Fall back to annualizing YTD
            ytd = stock.get("ytd_dividends", 0)
            if months_of_data > 0 and ytd > 0:
                forward_rate = (ytd / shares / months_of_data) * 12 if shares > 0 else 0
            else:
                forward_rate = 0

        projected_annual = forward_rate * shares
        dividend_yield = (
            (forward_rate / current_price * 100) if current_price > 0 else 0
        )

        # Get CAGR
        cagr = calculate_dividend_cagr(symbol)

        # Get dividend schedule
        frequency, payment_months = get_dividend_schedule(symbol)

        # Project with growth (apply CAGR for next year)
        if cagr is not None and cagr > -0.5:  # Ignore extreme negative CAGR
            projected_with_growth = projected_annual * (1 + cagr)
            cagr_values.append(cagr)
        else:
            projected_with_growth = projected_annual

        # Calculate per-payment amount and add to monthly breakdown
        if payment_months:
            per_payment = projected_annual / len(payment_months)
            growth_per_payment = (projected_with_growth - projected_annual) / len(
                payment_months
            )
            for month in payment_months:
                monthly_breakdown[month]["dividends"] += per_payment
                monthly_breakdown[month]["growth_bonus"] += growth_per_payment

        stock_projections.append(
            DividendProjection(
                symbol=symbol,
                shares=shares,
                current_price=current_price,
                forward_dividend_rate=forward_rate,
                projected_annual=projected_annual,
                dividend_yield=dividend_yield,
                cagr_5yr=cagr,
                projected_with_growth=projected_with_growth,
                frequency=frequency,
                payment_months=payment_months,
            )
        )

        total_dividend += projected_annual
        total_dividend_with_growth += projected_with_growth

    # Bond projections
    bond_projections = []
    total_bond = 0.0

    for bond in bond_list:
        symbol = bond["symbol"]
        bond_name = bond.get("bond_name", "")
        ytd_interest = bond.get("ytd_interest", 0)
        payment_count = bond.get("payment_count", 0)

        # Try to get coupon rate from bond name
        coupon_rate = parse_bond_coupon_rate(bond_name)

        # Get payment months for this bond
        payment_months = get_bond_payment_months(bond_name)

        # Estimate annual interest
        # Bonds typically pay semi-annually
        if payment_count > 0:
            per_payment = ytd_interest / payment_count
            annual_interest = per_payment * 2  # Semi-annual assumption
        else:
            annual_interest = ytd_interest

        # Add to monthly breakdown
        if payment_months:
            per_payment_amount = annual_interest / len(payment_months)
            for month in payment_months:
                monthly_breakdown[month]["bonds"] += per_payment_amount

        bond_projections.append(
            BondProjection(
                symbol=symbol,
                bond_name=bond_name,
                coupon_rate=coupon_rate or 0,
                annual_interest=annual_interest,
                payment_months=payment_months,
            )
        )

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
        monthly_breakdown=monthly_breakdown,
    )
