# Interactive Brokers Data Export Guide

## Overview
This guide walks you through the easiest way to export your options positions from Interactive Brokers for use with the Options Analyzer tool.

## Method 1: TWS Portfolio Export (Recommended for MVP)

### Step-by-Step Instructions

1. **Open Trader Workstation (TWS)**
   - Log into your IB account via TWS

2. **Navigate to Portfolio**
   - Click on the "Portfolio" tab in the main window
   - You should see all your current positions

3. **Export Portfolio to CSV**
   - Right-click anywhere in the portfolio table
   - Select "Export to File" or "Save As..."
   - Choose CSV format
   - Save the file to a known location (e.g., `~/Downloads/ib_positions.csv`)

4. **Verify Export Contents**
   The CSV should contain columns like:
   - Symbol
   - Description
   - Quantity
   - Strike
   - Expiry
   - Type (Call/Put)
   - Market Price
   - Market Value
   - Average Cost
   - Unrealized P&L
   - (Greeks may or may not be included)

### Expected CSV Format

```csv
Symbol,Description,Quantity,Strike,Expiry,Type,Market Price,Market Value,Average Cost,Unrealized P&L
AAPL,AAPL 19JAN24 150 C,-10,150,2024-01-19,CALL,1.80,-1800,2.50,700
MSFT,MSFT 16FEB24 380 P,-5,380,2024-02-16,PUT,3.20,-1600,4.10,450
...
```

## Method 2: Activity Statement (More Detailed)

### Step-by-Step Instructions

1. **Access Account Management**
   - Go to https://www.interactivebrokers.com
   - Log in to Account Management

2. **Navigate to Reports**
   - Click on "Reports" in the top menu
   - Select "Activity Statements"

3. **Generate Custom Statement**
   - Click "Create Custom Statement"
   - Select "Activity" statement type
   - Choose date range (today for current positions)
   - Under sections, select:
     - ✓ Open Positions
     - ✓ Mark-to-Market Performance Summary (for P&L)
   - Format: CSV
   - Click "Run"

4. **Download the Report**
   - Wait for the report to generate (usually < 1 minute)
   - Download the CSV file

### Expected Format
More detailed than portfolio export, includes:
- All position details
- Greeks (if available)
- Margin requirements
- P&L breakdown

## Method 3: Flex Queries (Most Flexible - Future)

Flex Queries allow you to create custom reports with exactly the fields you need. This is recommended for Phase 2 when we automate data retrieval.

### Setup (for future reference)

1. **Create Flex Query**
   - Account Management → Reports → Flex Queries
   - Create new "Activity Flex Query"
   - Select fields:
     - Symbol
     - Strike
     - Expiry
     - Put/Call
     - Quantity
     - Position Value
     - Mark Price
     - Delta
     - Gamma
     - Theta
     - Vega
     - Implied Volatility
     - Margin Requirement
   - Save query with a name (e.g., "OptionsAnalyzer")

2. **Generate Report**
   - Run the query manually or via API
   - Download CSV or XML

3. **API Integration (Phase 2)**
   - Use Flex Query API to automate retrieval
   - Schedule periodic updates

## Method 4: TWS API (Real-time - Phase 2)

For real-time data integration in Phase 2:

1. **Enable API in TWS**
   - File → Global Configuration → API → Settings
   - Enable ActiveX and Socket Clients
   - Note the Socket Port (default: 7497 for live, 7496 for paper)

2. **Use Python IB API**
   - Install: `pip install ib_insync`
   - Connect to TWS
   - Request position data and market data

This will be implemented in Phase 2 for real-time updates.

## Data Requirements for Options Analyzer

### Minimum Required Fields
- Symbol (underlying)
- Option Type (Call/Put)
- Strike Price
- Expiration Date
- Quantity (negative for short positions)
- Current Market Price
- Entry/Average Cost

### Highly Recommended Fields
- Delta
- Gamma
- Theta
- Vega
- Implied Volatility
- Underlying Price
- Margin Requirement

### Optional Fields (can be calculated)
- Probability of Profit
- Days to Expiration
- Unrealized P&L

## Troubleshooting

### Greeks Not Showing in Export
- **Solution 1**: Right-click portfolio table → Add columns → Select Greek columns
- **Solution 2**: Use Activity Statement instead of portfolio export
- **Solution 3**: Tool will calculate Greeks if not provided (Phase 1)

### Export File is Empty
- Ensure you have open positions
- Check that filters are not hiding positions
- Try Activity Statement method instead

### CSV Format Issues
- Ensure you're exporting as CSV, not Excel
- Check that delimiter is comma (not semicolon)
- Verify file encoding is UTF-8

### Missing Margin Information
- Margin may not be in portfolio export
- Use Activity Statement for margin data
- Tool will calculate margin if not provided

## Recommended Workflow for MVP

1. **Daily/Weekly Export**:
   - Open TWS
   - Export portfolio to CSV
   - Upload to Options Analyzer
   - Review recommendations
   - Take action on positions

2. **File Naming Convention**:
   - Use date in filename: `ib_positions_2024-01-15.csv`
   - Keep historical exports for reference

3. **Validation**:
   - After upload, verify position count matches TWS
   - Spot-check a few positions for accuracy
   - Compare margin calculations with IB's numbers

## Next Steps

Once you export your first file:
1. Share a sample (with sensitive data removed) to verify format
2. We'll build the parser to handle your specific CSV structure
3. Test import with your actual data
4. Iterate on any format issues

## Future Automation (Phase 2)

- Scheduled Flex Query execution
- Automatic file download
- Real-time API integration
- No manual export needed

