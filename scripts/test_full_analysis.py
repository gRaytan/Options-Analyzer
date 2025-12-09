#!/usr/bin/env python3
"""Test script to run the full analysis pipeline."""
import sys
import os

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.parser import IBActivityStatementParser
from src.data.price_fetcher import update_portfolio_prices
from src.data.storage import DataStorage
from src.analysis.greeks import calculate_portfolio_greeks
from src.analysis.margin import calculate_portfolio_margin
from src.analysis.risk import analyze_portfolio_risk


def main():
    filepath = "/Users/gilr/IdeaProjects/files/IBKR_content_2277971476944480583.plain"
    
    print("=" * 70)
    print("Full Options Analysis Pipeline")
    print("=" * 70)
    
    # Step 1: Parse the file
    print("\n📁 Step 1: Parsing IB Activity Statement...")
    parser = IBActivityStatementParser()
    portfolio = parser.parse_file(filepath)
    print(f"   ✅ Parsed {portfolio.total_option_positions} options, {portfolio.total_stock_positions} stocks")
    
    # Step 2: Fetch missing prices
    print("\n💰 Step 2: Fetching missing underlying prices...")
    update_portfolio_prices(portfolio)
    
    # Step 3: Calculate Greeks
    print("\n📊 Step 3: Calculating Greeks...")
    calculate_portfolio_greeks(portfolio, risk_free_rate=0.05)
    print(f"   ✅ Greeks calculated for all options")
    
    # Step 4: Calculate Margin
    print("\n💳 Step 4: Calculating Margin Requirements...")
    total_margin = calculate_portfolio_margin(portfolio, margin_type="reg_t")
    print(f"   ✅ Total Reg-T Margin: ${total_margin:,.2f}")
    
    # Step 5: Analyze Risk
    print("\n⚠️  Step 5: Analyzing Risk...")
    config = {
        "high_delta_threshold": 0.6,
        "days_to_expiration_warning": 7,
        "profit_target_percentage": 50,
    }
    risk_analysis = analyze_portfolio_risk(portfolio, config)
    print(f"   ✅ Risk analysis complete")
    
    # Print Summary
    print("\n" + "=" * 70)
    print("📈 PORTFOLIO SUMMARY")
    print("=" * 70)
    
    print(f"\n💼 Account: {portfolio.account_id} ({portfolio.account_name})")
    print(f"📅 Period: {portfolio.statement_period}")
    print(f"💵 Net Asset Value: ${portfolio.net_asset_value:,.2f}")
    print(f"💳 Total Margin Requirement: ${total_margin:,.2f}")
    print(f"📊 Margin Utilization: {risk_analysis.margin_utilization:.1%}")
    
    greeks = portfolio.portfolio_greeks
    print(f"\n📉 Portfolio Greeks:")
    print(f"   Delta: {greeks.delta:,.2f}")
    print(f"   Gamma: {greeks.gamma:,.4f}")
    print(f"   Theta: ${greeks.theta:,.2f}/day")
    print(f"   Vega: ${greeks.vega:,.2f}")
    
    print(f"\n⚠️  Risk Summary:")
    print(f"   Total Risk Score: {risk_analysis.summary['total_risk_score']}")
    print(f"   High Risk Positions: {risk_analysis.summary['high_risk_count']}")
    print(f"   High Delta Short Calls: {risk_analysis.summary['high_delta_calls']}")
    print(f"   ITM Options: {risk_analysis.summary['itm_options']}")
    print(f"   Expiring Soon (≤7 days): {risk_analysis.summary['expiring_soon']}")
    print(f"   Profit Targets Reached: {risk_analysis.summary['profit_targets_reached']}")
    
    # Show high risk positions
    if risk_analysis.high_risk_positions:
        print(f"\n🚨 HIGH RISK POSITIONS:")
        print("-" * 70)
        for pr in sorted(risk_analysis.high_risk_positions, key=lambda x: -x.risk_score)[:10]:
            opt = pr.option
            side = "SHORT" if opt.is_short else "LONG"
            opt_type = "CALL" if opt.is_call else "PUT"
            print(f"\n   {side} {abs(opt.quantity)} {opt.underlying} {opt.strike} {opt_type}")
            print(f"   Underlying: ${opt.underlying_price:.2f} | Delta: {opt.delta:.3f} | DTE: {opt.days_to_expiration}")
            print(f"   P&L: ${opt.unrealized_pnl:,.2f} | Risk Score: {pr.risk_score}")
            for flag in pr.flags:
                print(f"   ⚠️  [{flag.severity.upper()}] {flag.message}")
    
    # Show positions at profit target
    profit_positions = [pr for pr in risk_analysis.position_risks 
                        if any(f.flag_type == "PROFIT_TARGET_REACHED" for f in pr.flags)]
    if profit_positions:
        print(f"\n✅ POSITIONS AT PROFIT TARGET ({len(profit_positions)}):")
        print("-" * 70)
        for pr in profit_positions[:10]:
            opt = pr.option
            side = "SHORT" if opt.is_short else "LONG"
            opt_type = "CALL" if opt.is_call else "PUT"
            profit_flag = next(f for f in pr.flags if f.flag_type == "PROFIT_TARGET_REACHED")
            print(f"   {side} {abs(opt.quantity)} {opt.underlying} {opt.strike} {opt_type} - "
                  f"Profit: {profit_flag.value:.1f}% | P&L: ${opt.unrealized_pnl:,.2f}")
    
    # Save the analyzed portfolio
    print(f"\n💾 Saving analyzed portfolio...")
    storage = DataStorage()
    filepath = storage.save_portfolio(portfolio, "portfolio_analyzed.json")
    print(f"   Saved to: {filepath}")
    
    print("\n" + "=" * 70)
    print("Analysis complete!")
    print("=" * 70)


if __name__ == "__main__":
    main()

