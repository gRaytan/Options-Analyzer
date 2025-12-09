"""Fetch current stock prices for underlying symbols."""
import time
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


class PriceFetcher:
    """Fetch current stock prices using yfinance."""
    
    def __init__(self):
        self._cache: Dict[str, float] = {}
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
    
    def get_price(self, symbol: str) -> Optional[float]:
        """Get current price for a single symbol."""
        if symbol in self._cache:
            return self._cache[symbol]
        
        yf = self._get_yfinance()
        
        # Handle special symbols
        clean_symbol = self._clean_symbol(symbol)
        
        try:
            ticker = yf.Ticker(clean_symbol)
            # Try to get the current price
            info = ticker.info
            
            # Try different price fields
            price = info.get('regularMarketPrice') or info.get('currentPrice') or info.get('previousClose')
            
            if price:
                self._cache[symbol] = float(price)
                return float(price)
            
            # Fallback: try to get from history
            hist = ticker.history(period="1d")
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
                self._cache[symbol] = price
                return price
                
        except Exception as e:
            print(f"Warning: Could not fetch price for {symbol}: {e}")
        
        return None
    
    def get_prices(self, symbols: List[str], max_workers: int = 5) -> Dict[str, float]:
        """Get prices for multiple symbols in parallel."""
        yf = self._get_yfinance()
        
        # Filter out symbols we already have cached
        symbols_to_fetch = [s for s in symbols if s not in self._cache]
        
        if not symbols_to_fetch:
            return {s: self._cache[s] for s in symbols if s in self._cache}
        
        # Clean symbols
        clean_symbols = [self._clean_symbol(s) for s in symbols_to_fetch]
        symbol_map = dict(zip(clean_symbols, symbols_to_fetch))
        
        # Batch download
        try:
            # Use yfinance download for batch fetching
            data = yf.download(
                tickers=clean_symbols,
                period="1d",
                progress=False,
                threads=True
            )
            
            if not data.empty:
                # Handle single vs multiple symbols
                if len(clean_symbols) == 1:
                    if 'Close' in data.columns:
                        price = float(data['Close'].iloc[-1])
                        original_symbol = symbols_to_fetch[0]
                        self._cache[original_symbol] = price
                else:
                    # Multiple symbols - data has multi-level columns
                    if 'Close' in data.columns:
                        close_prices = data['Close']
                        for clean_sym in clean_symbols:
                            if clean_sym in close_prices.columns:
                                price = close_prices[clean_sym].iloc[-1]
                                if not pd.isna(price):
                                    original_symbol = symbol_map[clean_sym]
                                    self._cache[original_symbol] = float(price)
        except Exception as e:
            print(f"Warning: Batch download failed: {e}")
            # Fallback to individual fetching
            for symbol in symbols_to_fetch:
                self.get_price(symbol)
        
        # Return all requested prices
        return {s: self._cache.get(s) for s in symbols if s in self._cache}
    
    def _clean_symbol(self, symbol: str) -> str:
        """Clean symbol for yfinance compatibility."""
        # Handle special cases
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
        """Clear the price cache."""
        self._cache.clear()
    
    def get_cached_prices(self) -> Dict[str, float]:
        """Get all cached prices."""
        return self._cache.copy()


def update_portfolio_prices(portfolio, fetcher: Optional[PriceFetcher] = None) -> Dict[str, float]:
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

