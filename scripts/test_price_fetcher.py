#!/usr/bin/env python3
"""Test script to verify the price fetcher works."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.parser import IBActivityStatementParser
from src.data.price_fetcher import PriceFetcher, update_portfolio_prices


def main():
    # Path to the IB statement file
    filepath = "/Users/gilr/IdeaProjects/files/IBKR_content_2277971476944480583.plain"
    
    print("=" * 60)
    print("Price Fetcher Test")
    print("=" * 60)
    
    # Parse the file
    parser = IBActivityStatementParser()
    portfolio = parser.parse_file(filepath)
    
    # Count options without underlying prices
    options_without_price = [opt for opt in portfolio.options if opt.underlying_price == 0]
    underlyings_needing_prices = set(opt.underlying for opt in options_without_price)
    
    print(f"\n📊 Before fetching prices:")
    print(f"   Options without underlying price: {len(options_without_price)}")
    print(f"   Unique underlyings needing prices: {len(underlyings_needing_prices)}")
    
    # Test fetching a few prices individually
    print(f"\n🔍 Testing individual price fetch...")
    fetcher = PriceFetcher()
    
    test_symbols = ["AAPL", "GOOGL", "META", "BRK B", "NVDA"]
    for symbol in test_symbols:
        price = fetcher.get_price(symbol)
        if price:
            print(f"   {symbol}: ${price:.2f}")
        else:
            print(f"   {symbol}: Failed to fetch")
    
    # Now update all portfolio prices
    print(f"\n📈 Fetching all missing underlying prices...")
    prices = update_portfolio_prices(portfolio, fetcher)
    
    print(f"\n✅ Fetched prices for {len(prices)} underlyings")
    
    # Count again
    options_still_without_price = [opt for opt in portfolio.options if opt.underlying_price == 0]
    print(f"\n📊 After fetching prices:")
    print(f"   Options still without underlying price: {len(options_still_without_price)}")
    
    if options_still_without_price:
        missing_underlyings = set(opt.underlying for opt in options_still_without_price)
        print(f"   Underlyings still missing: {missing_underlyings}")
    
    # Show some sample options with prices now
    print(f"\n📋 Sample options with updated prices:")
    for opt in portfolio.options[:10]:
        if opt.underlying_price > 0:
            side = "SHORT" if opt.is_short else "LONG"
            opt_type = "CALL" if opt.is_call else "PUT"
            print(f"   {side} {abs(opt.quantity)} {opt.underlying} {opt.strike} {opt_type}")
            print(f"      Underlying: ${opt.underlying_price:.2f} | Option: ${opt.current_price:.2f} | DTE: {opt.days_to_expiration}")
    
    print("\n" + "=" * 60)
    print("Price fetcher test complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()

