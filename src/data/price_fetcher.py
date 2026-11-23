"""Fetch current stock prices for underlying symbols."""
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


# Global cache that persists across requests
_global_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()
_last_request_time = 0
_request_lock = threading.Lock()

# Cache expiry time (1 hour)
CACHE_EXPIRY_SECONDS = 3600
# Minimum time between requests (to avoid rate limiting)
MIN_REQUEST_INTERVAL = 0.5  # 500ms between requests


class PriceFetcher:
    """Fetch current stock prices using yfinance with rate limiting and caching."""

    def __init__(self):
        self._yf = None

    def _get_yfinance(self):
        """Lazy load yfinance."""
        if self._yf is None:
            try:
                import yfinance as yf

                self._yf = yf
            except ImportError:
                raise ImportError(
                    "yfinance is required for price fetching. "
                    "Install it with: pip install yfinance"
                )
        return self._yf

    def _rate_limit(self):
        """Enforce rate limiting between requests."""
        global _last_request_time
        with _request_lock:
            now = time.time()
            elapsed = now - _last_request_time
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
            _last_request_time = time.time()

    def _get_from_cache(self, symbol: str) -> Optional[float]:
        """Get price from global cache if not expired."""
        with _cache_lock:
            if symbol in _global_cache:
                entry = _global_cache[symbol]
                if datetime.now() - entry["timestamp"] < timedelta(
                    seconds=CACHE_EXPIRY_SECONDS
                ):
                    return entry.get("price")
        return None

    def _set_cache(self, symbol: str, price: float, extra_data: Dict = None):
        """Set price in global cache."""
        with _cache_lock:
            _global_cache[symbol] = {
                "price": price,
                "timestamp": datetime.now(),
                **(extra_data or {}),
            }

    def get_price(self, symbol: str, retry_count: int = 2) -> Optional[float]:
        """Get current price for a single symbol with retry logic."""
        # Check cache first
        cached = self._get_from_cache(symbol)
        if cached is not None:
            return cached

        yf = self._get_yfinance()
        clean_symbol = self._clean_symbol(symbol)

        for attempt in range(retry_count + 1):
            try:
                self._rate_limit()

                ticker = yf.Ticker(clean_symbol)
                # Try fast_info first (less API calls)
                try:
                    price = ticker.fast_info.get("lastPrice") or ticker.fast_info.get(
                        "previousClose"
                    )
                    if price and price > 0:
                        self._set_cache(symbol, float(price))
                        return float(price)
                except Exception:
                    pass

                # Fallback to history (more reliable)
                hist = ticker.history(period="1d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
                    self._set_cache(symbol, price)
                    return price

            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    # Rate limited - wait and retry
                    wait_time = (attempt + 1) * 2  # Exponential backoff
                    print(f"Rate limited for {symbol}, waiting {wait_time}s...")
                    time.sleep(wait_time)
                elif attempt == retry_count:
                    # Only log on final attempt
                    print(f"Warning: Could not fetch price for {symbol}: {e}")

        return None

    def get_prices(self, symbols: List[str], max_workers: int = 3) -> Dict[str, float]:
        """Get prices for multiple symbols with rate limiting."""
        yf = self._get_yfinance()

        result = {}
        symbols_to_fetch = []

        # Check cache first
        for s in symbols:
            cached = self._get_from_cache(s)
            if cached is not None:
                result[s] = cached
            else:
                symbols_to_fetch.append(s)

        if not symbols_to_fetch:
            return result

        # Clean symbols
        clean_symbols = [self._clean_symbol(s) for s in symbols_to_fetch]
        symbol_map = dict(zip(clean_symbols, symbols_to_fetch))

        # Batch download with retry
        for attempt in range(3):
            try:
                self._rate_limit()

                # Use yfinance download for batch fetching
                data = yf.download(
                    tickers=clean_symbols,
                    period="1d",
                    progress=False,
                    threads=False,  # Disable threading to reduce rate limit issues
                )

                if not data.empty:
                    # Handle single vs multiple symbols
                    if len(clean_symbols) == 1:
                        if "Close" in data.columns:
                            price = float(data["Close"].iloc[-1])
                            original_symbol = symbols_to_fetch[0]
                            self._set_cache(original_symbol, price)
                            result[original_symbol] = price
                    else:
                        # Multiple symbols - data has multi-level columns
                        if "Close" in data.columns:
                            close_prices = data["Close"]
                            for clean_sym in clean_symbols:
                                if clean_sym in close_prices.columns:
                                    price = close_prices[clean_sym].iloc[-1]
                                    if not pd.isna(price):
                                        original_symbol = symbol_map[clean_sym]
                                        self._set_cache(original_symbol, float(price))
                                        result[original_symbol] = float(price)
                break  # Success, exit retry loop

            except Exception as e:
                if "429" in str(e) or "Too Many Requests" in str(e):
                    wait_time = (attempt + 1) * 5
                    print(f"Rate limited on batch download, waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"Warning: Batch download failed: {e}")
                    break

        return result

    def _clean_symbol(self, symbol: str) -> str:
        """Clean symbol for yfinance compatibility."""
        symbol = symbol.strip()

        # BRK B -> BRK-B (Berkshire Hathaway)
        if symbol == "BRK B":
            return "BRK-B"

        # Handle other class shares
        if " " in symbol:
            parts = symbol.split()
            if len(parts) == 2 and len(parts[1]) == 1:
                return f"{parts[0]}-{parts[1]}"

        return symbol

    def clear_cache(self):
        """Clear the global price cache."""
        global _global_cache
        with _cache_lock:
            _global_cache.clear()

    def get_cached_prices(self) -> Dict[str, float]:
        """Get all cached prices."""
        with _cache_lock:
            return {k: v["price"] for k, v in _global_cache.items() if "price" in v}


def get_global_cache() -> Dict[str, Any]:
    """Get the global cache for inspection."""
    with _cache_lock:
        return _global_cache.copy()


def update_portfolio_prices(
    portfolio, fetcher: Optional[PriceFetcher] = None
) -> Dict[str, float]:
    """Update underlying prices for all options in a portfolio."""
    if fetcher is None:
        fetcher = PriceFetcher()

    # Get unique underlyings that need prices
    underlyings_needing_prices = set()
    for option in portfolio.options:
        if option.underlying_price == 0 and option.underlying:
            underlyings_needing_prices.add(option.underlying)

    if not underlyings_needing_prices:
        return {}

    print(f"Fetching prices for {len(underlyings_needing_prices)} underlyings...")

    # Fetch prices
    prices = fetcher.get_prices(list(underlyings_needing_prices))

    # Update options
    updated_count = 0
    for option in portfolio.options:
        if option.underlying_price == 0 and option.underlying in prices:
            price = prices[option.underlying]
            if price:
                option.underlying_price = price
                updated_count += 1

    print(f"Updated {updated_count} options with underlying prices")

    return prices
