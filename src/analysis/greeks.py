"""Greeks calculation using Black-Scholes model."""
import math
from typing import Optional, Tuple
from datetime import date

from src.models.position import OptionPosition, OptionType
from src.models.portfolio import Portfolio


class GreeksCalculator:
    """Calculate option Greeks using Black-Scholes model."""

    def __init__(self, risk_free_rate: float = 0.05):
        """
        Initialize the Greeks calculator.

        Args:
            risk_free_rate: Annual risk-free interest rate (default 5%)
        """
        self.risk_free_rate = risk_free_rate

    def calculate_greeks(
        self, option: OptionPosition
    ) -> Tuple[float, float, float, float, float]:
        """
        Calculate all Greeks for an option position.

        Returns:
            Tuple of (delta, gamma, theta, vega, implied_volatility)
        """
        # Validate inputs
        if option.underlying_price <= 0 or option.strike <= 0:
            return (0.0, 0.0, 0.0, 0.0, 0.0)

        if option.days_to_expiration <= 0:
            # Expired option
            return self._expired_greeks(option)

        # Calculate implied volatility from option price
        iv = self._calculate_implied_volatility(option)

        if iv <= 0:
            return (0.0, 0.0, 0.0, 0.0, 0.0)

        # Calculate Greeks
        S = option.underlying_price
        K = option.strike
        T = option.days_to_expiration / 365.0
        r = self.risk_free_rate
        sigma = iv

        d1, d2 = self._calculate_d1_d2(S, K, T, r, sigma)

        # Delta
        if option.is_call:
            delta = self._norm_cdf(d1)
        else:
            delta = self._norm_cdf(d1) - 1

        # Gamma (same for calls and puts)
        gamma = self._norm_pdf(d1) / (S * sigma * math.sqrt(T))

        # Theta (per day)
        theta = self._calculate_theta(S, K, T, r, sigma, d1, d2, option.is_call)

        # Vega (per 1% change in volatility)
        vega = S * self._norm_pdf(d1) * math.sqrt(T) / 100

        return (delta, gamma, theta, vega, iv)

    def _expired_greeks(
        self, option: OptionPosition
    ) -> Tuple[float, float, float, float, float]:
        """Calculate Greeks for expired options."""
        if option.is_call:
            delta = 1.0 if option.underlying_price > option.strike else 0.0
        else:
            delta = -1.0 if option.underlying_price < option.strike else 0.0
        return (delta, 0.0, 0.0, 0.0, 0.0)

    def _calculate_implied_volatility(
        self,
        option: OptionPosition,
        max_iterations: int = 100,
        tolerance: float = 0.0001,
    ) -> float:
        """Calculate implied volatility using Newton-Raphson method."""
        S = option.underlying_price
        K = option.strike
        T = option.days_to_expiration / 365.0
        r = self.risk_free_rate
        market_price = option.current_price
        is_call = option.is_call

        if T <= 0 or market_price <= 0:
            return 0.0

        # Initial guess
        sigma = 0.3

        for _ in range(max_iterations):
            price = self._black_scholes_price(S, K, T, r, sigma, is_call)
            vega = self._calculate_vega(S, K, T, r, sigma)

            if vega < 0.0001:
                break

            diff = market_price - price
            if abs(diff) < tolerance:
                return sigma

            sigma = sigma + diff / vega

            # Keep sigma in reasonable bounds
            sigma = max(0.01, min(sigma, 5.0))

        return sigma

    def _black_scholes_price(
        self, S: float, K: float, T: float, r: float, sigma: float, is_call: bool
    ) -> float:
        """Calculate Black-Scholes option price."""
        d1, d2 = self._calculate_d1_d2(S, K, T, r, sigma)

        if is_call:
            price = S * self._norm_cdf(d1) - K * math.exp(-r * T) * self._norm_cdf(d2)
        else:
            price = K * math.exp(-r * T) * self._norm_cdf(-d2) - S * self._norm_cdf(-d1)

        return price

    def _calculate_d1_d2(
        self, S: float, K: float, T: float, r: float, sigma: float
    ) -> Tuple[float, float]:
        """Calculate d1 and d2 for Black-Scholes."""
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        return d1, d2

    def _calculate_vega(
        self, S: float, K: float, T: float, r: float, sigma: float
    ) -> float:
        """Calculate vega for Newton-Raphson."""
        d1, _ = self._calculate_d1_d2(S, K, T, r, sigma)
        return S * self._norm_pdf(d1) * math.sqrt(T)

    def _calculate_theta(
        self,
        S: float,
        K: float,
        T: float,
        r: float,
        sigma: float,
        d1: float,
        d2: float,
        is_call: bool,
    ) -> float:
        """Calculate theta (per day)."""
        term1 = -(S * self._norm_pdf(d1) * sigma) / (2 * math.sqrt(T))

        if is_call:
            term2 = -r * K * math.exp(-r * T) * self._norm_cdf(d2)
        else:
            term2 = r * K * math.exp(-r * T) * self._norm_cdf(-d2)

        # Convert to daily theta
        return (term1 + term2) / 365

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Standard normal cumulative distribution function."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    @staticmethod
    def _norm_pdf(x: float) -> float:
        """Standard normal probability density function."""
        return math.exp(-0.5 * x**2) / math.sqrt(2 * math.pi)


def calculate_portfolio_greeks(
    portfolio: Portfolio, risk_free_rate: float = 0.05
) -> None:
    """Calculate and update Greeks for all options in a portfolio."""
    calculator = GreeksCalculator(risk_free_rate)

    for option in portfolio.options:
        delta, gamma, theta, vega, iv = calculator.calculate_greeks(option)
        option.delta = delta
        option.gamma = gamma
        option.theta = theta
        option.vega = vega
        option.implied_volatility = iv
