#!/usr/bin/env python3
"""Test script to verify the IB Activity Statement parser."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.parser import IBActivityStatementParser
from src.data.storage import DataStorage


def main():
    # Path to the IB statement file
    filepath = "/Users/gilr/IdeaProjects/files/IBKR_content_2277971476944480583.plain"
    
    print("=" * 60)
    print("IB Activity Statement Parser Test")
    print("=" * 60)
    
    # Parse the file
    parser = IBActivityStatementParser()
    portfolio = parser.parse_file(filepath)
    
    # Print summary
    print(f"\n📊 Portfolio Summary")
    print("-" * 40)
    print(f"Account: {portfolio.account_id} ({portfolio.account_name})")
    print(f"Period: {portfolio.statement_period}")
    print(f"Base Currency: {portfolio.base_currency}")
    print(f"Net Asset Value: ${portfolio.net_asset_value:,.2f}")
    print(f"Cash Balance: ${portfolio.cash_balance:,.2f}")
    
    print(f"\n📈 Positions")
    print("-" * 40)
    print(f"Total Stock Positions: {portfolio.total_stock_positions}")
    print(f"Total Option Positions: {portfolio.total_option_positions}")
    print(f"  - Short Options: {len(portfolio.short_options)}")
    print(f"  - Long Options: {len(portfolio.long_options)}")
    print(f"  - Short Calls: {len(portfolio.short_calls)}")
    print(f"  - Short Puts: {len(portfolio.short_puts)}")
    
    print(f"\n💰 P&L Summary")
    print("-" * 40)
    print(f"Stocks Unrealized P&L: ${portfolio.stocks_unrealized_pnl:,.2f}")
    print(f"Options Unrealized P&L: ${portfolio.options_unrealized_pnl:,.2f}")
    print(f"Total Unrealized P&L: ${portfolio.total_unrealized_pnl:,.2f}")
    
    print(f"\n📋 Sample Stock Positions (first 5)")
    print("-" * 40)
    for stock in portfolio.stocks[:5]:
        print(f"  {stock.symbol}: {stock.quantity} shares @ ${stock.current_price:.2f} "
              f"(P&L: ${stock.unrealized_pnl:,.2f})")
    
    print(f"\n📋 Sample Option Positions (first 10)")
    print("-" * 40)
    for opt in portfolio.options[:10]:
        side = "SHORT" if opt.is_short else "LONG"
        opt_type = "CALL" if opt.is_call else "PUT"
        exp_str = opt.expiration.strftime("%Y-%m-%d") if opt.expiration else "N/A"
        underlying_str = f"@ ${opt.underlying_price:.2f}" if opt.underlying_price > 0 else "(no underlying price)"
        print(f"  {side} {abs(opt.quantity)} {opt.underlying} {opt.strike} {opt_type} exp {exp_str}")
        print(f"    Price: ${opt.current_price:.2f} | P&L: ${opt.unrealized_pnl:,.2f} | DTE: {opt.days_to_expiration} | {underlying_str}")
    
    # Find options without underlying prices
    options_without_price = [opt for opt in portfolio.options if opt.underlying_price == 0]
    print(f"\n⚠️  Options without underlying price: {len(options_without_price)}")
    
    # Get unique underlyings without prices
    underlyings_without_price = set(opt.underlying for opt in options_without_price)
    print(f"   Underlyings needing price data: {len(underlyings_without_price)}")
    if underlyings_without_price:
        print(f"   Examples: {list(underlyings_without_price)[:10]}")
    
    # Options expiring soon
    expiring_soon = portfolio.get_options_expiring_soon(days=14)
    print(f"\n⏰ Options expiring within 14 days: {len(expiring_soon)}")
    for opt in expiring_soon[:5]:
        side = "SHORT" if opt.is_short else "LONG"
        opt_type = "CALL" if opt.is_call else "PUT"
        print(f"  {side} {abs(opt.quantity)} {opt.underlying} {opt.strike} {opt_type} - DTE: {opt.days_to_expiration}")
    
    # Save to JSON
    print(f"\n💾 Saving portfolio to JSON...")
    storage = DataStorage()
    filepath = storage.save_portfolio(portfolio)
    print(f"   Saved to: {filepath}")
    
    # Verify we can load it back
    print(f"\n🔄 Verifying JSON load...")
    loaded = storage.load_portfolio()
    if loaded:
        print(f"   ✅ Successfully loaded {loaded.total_option_positions} options and {loaded.total_stock_positions} stocks")
    else:
        print(f"   ❌ Failed to load portfolio")
    
    print("\n" + "=" * 60)
    print("Parser test complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()

