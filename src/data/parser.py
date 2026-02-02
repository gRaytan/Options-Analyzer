"""Parser for Interactive Brokers Activity Statement CSV files."""
import csv
import re
from datetime import datetime, date
from typing import List, Dict, Tuple, Optional
from io import StringIO

from src.models.position import OptionPosition, StockPosition, OptionType, Trade, TradeAction, Dividend, BondInterest, CashTransaction
from src.models.portfolio import Portfolio


class IBActivityStatementParser:
    """Parser for IB Activity Statement CSV exports."""
    
    def __init__(self):
        self.sections: Dict[str, List[Dict]] = {}
        
    def parse_file(self, filepath: str) -> Portfolio:
        """Parse an IB Activity Statement CSV file."""
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            content = f.read()
        return self.parse_content(content)
    
    def parse_content(self, content: str) -> Portfolio:
        """Parse CSV content string."""
        self._extract_sections(content)
        
        portfolio = Portfolio()
        
        # Parse account info
        self._parse_account_info(portfolio)
        
        # Parse NAV
        self._parse_nav(portfolio)
        
        # Parse stock positions
        portfolio.stocks = self._parse_stock_positions()
        
        # Parse option positions
        portfolio.options = self._parse_option_positions()
        
        # Enrich options with financial instrument info
        self._enrich_options_with_instrument_info(portfolio.options)

        # Set underlying prices from stock positions
        self._set_underlying_prices(portfolio)

        # Parse trades
        portfolio.trades = self._parse_trades()

        # Parse dividends
        portfolio.dividends = self._parse_dividends()

        # Parse bond interest/coupons
        portfolio.bond_interest = self._parse_bond_interest()

        # Parse deposits and withdrawals
        portfolio.cash_transactions = self._parse_deposits_withdrawals()

        # Parse margin/cash report data
        self._parse_cash_report(portfolio)

        return portfolio
    
    def _extract_sections(self, content: str) -> None:
        """Extract all sections from the CSV content."""
        self.sections = {}
        reader = csv.reader(StringIO(content))

        current_section = None
        current_headers = []

        for row in reader:
            if len(row) < 2:
                continue

            section_name = row[0]
            row_type = row[1]

            if section_name not in self.sections:
                self.sections[section_name] = []

            if row_type == "Header":
                current_section = section_name
                current_headers = row[2:]  # Skip section name and "Header"
            elif row_type == "Data":
                # Handle sections without Header rows (e.g., Statement section)
                # Format: SectionName,Data,FieldName,FieldValue
                if len(row) >= 4 and section_name == "Statement":
                    data = {"Field Name": row[2], "Field Value": row[3]}
                    data['_section'] = section_name
                    self.sections[section_name].append(data)
                elif current_headers:
                    data = dict(zip(current_headers, row[2:]))
                    data['_section'] = section_name
                    self.sections[section_name].append(data)
    
    def _parse_account_info(self, portfolio: Portfolio) -> None:
        """Parse account information section."""
        account_info = self.sections.get("Account Information", [])
        for item in account_info:
            field_name = item.get("Field Name", "")
            field_value = item.get("Field Value", "")
            
            if field_name == "Account":
                portfolio.account_id = field_value
            elif field_name == "Name":
                portfolio.account_name = field_value
            elif field_name == "Base Currency":
                portfolio.base_currency = field_value
        
        # Parse statement period
        statement_info = self.sections.get("Statement", [])
        for item in statement_info:
            if item.get("Field Name") == "Period":
                period_str = item.get("Field Value", "")
                portfolio.statement_period = period_str
                # Parse start and end dates from period string
                # Format: "January 1, 2026 - January 31, 2026"
                portfolio.period_start, portfolio.period_end = self._parse_period_dates(period_str)

    def _parse_period_dates(self, period_str: str) -> Tuple[Optional[date], Optional[date]]:
        """Parse statement period string into start and end dates."""
        if not period_str or " - " not in period_str:
            return None, None

        try:
            parts = period_str.split(" - ")
            if len(parts) != 2:
                return None, None

            # Parse dates like "January 1, 2026"
            start_date = datetime.strptime(parts[0].strip(), "%B %d, %Y").date()
            end_date = datetime.strptime(parts[1].strip(), "%B %d, %Y").date()
            return start_date, end_date
        except ValueError:
            return None, None
    
    def _parse_nav(self, portfolio: Portfolio) -> None:
        """Parse Net Asset Value section."""
        nav_data = self.sections.get("Net Asset Value", [])
        for item in nav_data:
            asset_class = item.get("Asset Class", "")
            if asset_class == "Total":
                portfolio.net_asset_value = self._parse_float(item.get("Current Total", "0"))
            elif asset_class == "Cash ":  # Note: IB has trailing space
                portfolio.cash_balance = self._parse_float(item.get("Current Total", "0"))

    def _parse_cash_report(self, portfolio: Portfolio) -> None:
        """Parse Cash Report section for margin data."""
        cash_report = self.sections.get("Cash Report", [])

        # Map IBKR field names to portfolio attributes
        field_mapping = {
            "Excess Liquidity": "excess_liquidity",
            "Buying Power": "buying_power",
            "Maintenance Margin Requirement": "maintenance_margin",
            "Maintenance Margin": "maintenance_margin",
            "Initial Margin Requirement": "initial_margin",
            "Initial Margin": "initial_margin",
            "SMA": "sma",
            "Special Memorandum Account": "sma",
            "Available Funds": "available_funds",
            "Gross Position Value": "gross_position_value",
            "Net Liquidation Value": "net_liquidation_value",
            "Net Liquidation": "net_liquidation_value",
        }

        for item in cash_report:
            # Cash Report uses different column names depending on format
            field_name = item.get("Currency Summary", "") or item.get("Field Name", "")
            # Try to get value from different possible column names
            value = (item.get("Total", "") or item.get("Current Total", "") or
                     item.get("Field Value", "") or item.get("Base Currency Summary", ""))

            if field_name in field_mapping:
                attr_name = field_mapping[field_name]
                parsed_value = self._parse_float(value)
                # Only set if we got a valid value and it's larger than current
                # (in case of multiple currencies, we want the base currency total)
                current_value = getattr(portfolio, attr_name, 0.0)
                if parsed_value != 0 and (current_value == 0 or "Total" in str(item)):
                    setattr(portfolio, attr_name, parsed_value)

    def _parse_stock_positions(self) -> List[StockPosition]:
        """Parse stock positions from Open Positions section."""
        positions = []
        open_positions = self.sections.get("Open Positions", [])
        
        for item in open_positions:
            if item.get("Asset Category") != "Stocks":
                continue
            if item.get("DataDiscriminator") != "Summary":
                continue
                
            try:
                position = StockPosition(
                    symbol=item.get("Symbol", ""),
                    quantity=self._parse_int(item.get("Quantity", "0")),
                    cost_price=self._parse_float(item.get("Cost Price", "0")),
                    current_price=self._parse_float(item.get("Close Price", "0")),
                    currency=item.get("Currency", "USD"),
                )
                positions.append(position)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse stock position: {e}")
                
        return positions
    
    def _parse_option_positions(self) -> List[OptionPosition]:
        """Parse option positions from Open Positions section."""
        positions = []
        open_positions = self.sections.get("Open Positions", [])
        
        for item in open_positions:
            if item.get("Asset Category") != "Equity and Index Options":
                continue
            if item.get("DataDiscriminator") != "Summary":
                continue
                
            try:
                symbol = item.get("Symbol", "")
                underlying, strike, expiration, opt_type = self._parse_option_symbol(symbol)
                
                position = OptionPosition(
                    symbol=symbol,
                    underlying=underlying,
                    strike=strike,
                    expiration=expiration,
                    option_type=opt_type,
                    quantity=self._parse_int(item.get("Quantity", "0")),
                    cost_price=self._parse_float(item.get("Cost Price", "0")),
                    current_price=self._parse_float(item.get("Close Price", "0")),
                    currency=item.get("Currency", "USD"),
                )
                positions.append(position)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse option position {item.get('Symbol', 'unknown')}: {e}")
                
        return positions
    
    def _parse_option_symbol(self, symbol: str) -> Tuple[str, float, Optional[date], Optional[OptionType]]:
        """Parse option symbol like 'AAPL 15JAN27 110 P' or 'BRK B 18JUN26 240 P' into components."""
        # Pattern: UNDERLYING DDMMMYY STRIKE TYPE (underlying may have spaces)
        # We parse from the end since the last 3 parts are always: DATE STRIKE TYPE
        parts = symbol.strip().split()

        if len(parts) < 4:
            return symbol, 0.0, None, None

        # Last part is option type (C or P)
        opt_type_str = parts[-1]
        if opt_type_str not in ('C', 'P'):
            return symbol, 0.0, None, None
        opt_type = OptionType.CALL if opt_type_str == 'C' else OptionType.PUT

        # Second to last is strike
        try:
            strike = float(parts[-2])
        except ValueError:
            return symbol, 0.0, None, None

        # Third to last is date (DDMMMYY format)
        date_str = parts[-3]
        expiration = self._parse_expiry_string(date_str)

        # Everything before that is the underlying (handles "BRK B" etc.)
        underlying = " ".join(parts[:-3])

        return underlying, strike, expiration, opt_type

    def _parse_expiry_string(self, date_str: str) -> Optional[date]:
        """Parse expiry string like '15JAN27' to date."""
        pattern = r'^(\d{1,2})([A-Z]{3})(\d{2})$'
        match = re.match(pattern, date_str.strip())

        if not match:
            return None

        day = int(match.group(1))
        month_str = match.group(2)
        year = int(match.group(3)) + 2000

        months = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
                  'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}
        month = months.get(month_str, 1)

        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _enrich_options_with_instrument_info(self, options: List[OptionPosition]) -> None:
        """Enrich options with data from Financial Instrument Information section."""
        instrument_info = self.sections.get("Financial Instrument Information", [])

        # Build lookup by symbol
        info_lookup = {}
        for item in instrument_info:
            if item.get("Asset Category") == "Equity and Index Options":
                # The Description field contains the readable symbol
                desc = item.get("Description", "")
                info_lookup[desc] = item

        for option in options:
            info = info_lookup.get(option.symbol)
            if info:
                # Update expiration from more reliable source
                expiry_str = info.get("Expiry", "")
                if expiry_str:
                    try:
                        option.expiration = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                    except ValueError:
                        pass

                # Update strike if available
                strike_str = info.get("Strike", "")
                if strike_str:
                    try:
                        option.strike = float(strike_str)
                    except ValueError:
                        pass

                # Update underlying
                underlying = info.get("Underlying", "")
                if underlying:
                    option.underlying = underlying

                # Update option type
                opt_type = info.get("Type", "")
                if opt_type == "P":
                    option.option_type = OptionType.PUT
                elif opt_type == "C":
                    option.option_type = OptionType.CALL

    def _set_underlying_prices(self, portfolio: Portfolio) -> None:
        """Set underlying prices for options from stock positions."""
        stock_prices = {stock.symbol: stock.current_price for stock in portfolio.stocks}

        for option in portfolio.options:
            if option.underlying in stock_prices:
                option.underlying_price = stock_prices[option.underlying]

    def _parse_trades(self) -> List[Trade]:
        """Parse trades from Trades section."""
        trades = []
        trades_data = self.sections.get("Trades", [])

        for item in trades_data:
            if item.get("DataDiscriminator") != "Order":
                continue

            asset_category = item.get("Asset Category", "")
            is_option = asset_category == "Equity and Index Options"

            try:
                symbol = item.get("Symbol", "")

                # Parse date/time
                date_str = item.get("Date/Time", "")
                trade_date = self._parse_trade_datetime(date_str)

                # Parse action from Code field (O=Open, C=Close, Ep=Expired, A=Assigned)
                code = item.get("Code", "")
                action = self._parse_trade_action(code)

                # Parse option details if applicable
                underlying = ""
                strike = 0.0
                expiration = None
                opt_type = None

                if is_option:
                    underlying, strike, expiration, opt_type = self._parse_option_symbol(symbol)

                trade = Trade(
                    symbol=symbol,
                    trade_date=trade_date,
                    quantity=self._parse_int(item.get("Quantity", "0")),
                    price=self._parse_float(item.get("T. Price", "0")),
                    proceeds=self._parse_float(item.get("Proceeds", "0")),
                    commission=self._parse_float(item.get("Comm/Fee", "0")),
                    realized_pnl=self._parse_float(item.get("Realized P/L", "0")),
                    asset_type="Option" if is_option else "Stock",
                    underlying=underlying,
                    strike=strike,
                    expiration=expiration,
                    option_type=opt_type,
                    action=action,
                )
                trades.append(trade)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse trade: {e}")

        return trades

    def _parse_dividends(self) -> List[Dividend]:
        """Parse dividends from Dividends section."""
        dividends = []
        dividends_data = self.sections.get("Dividends", [])

        for item in dividends_data:
            try:
                # Parse date
                date_str = item.get("Date", "")
                if not date_str:
                    continue
                payment_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()

                # Parse description to extract symbol
                description = item.get("Description", "")
                # Format: "JEPI(US46641Q3323) Cash Dividend USD 0.39478 per Share (Ordinary Dividend)"
                symbol_match = re.match(r'^([A-Z]+)\(', description)
                symbol = symbol_match.group(1) if symbol_match else ""

                # Parse amount
                amount = self._parse_float(item.get("Amount", "0"))

                dividend = Dividend(
                    symbol=symbol,
                    payment_date=payment_date,
                    amount=amount,
                    description=description,
                    currency=item.get("Currency", "USD"),
                )
                dividends.append(dividend)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse dividend: {e}")

        return dividends

    def _parse_bond_interest(self) -> List[BondInterest]:
        """Parse bond coupon payments from Interest section."""
        interest_payments = []
        interest_data = self.sections.get("Interest", [])

        for item in interest_data:
            try:
                description = item.get("Description", "")

                # Only parse bond coupon payments
                # Format: "Bond Coupon Payment (AMZN 4.95 12/05/44 - Amazon.com Inc AMZN 4.95 12/05/44)"
                if not description.startswith("Bond Coupon Payment"):
                    continue

                # Parse date
                date_str = item.get("Date", "")
                if not date_str:
                    continue
                payment_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()

                # Parse amount
                amount = self._parse_float(item.get("Amount", "0"))
                if amount <= 0:
                    continue

                # Extract symbol and bond name from description
                # Format: "Bond Coupon Payment (AMZN 4.95 12/05/44 - Amazon.com Inc AMZN 4.95 12/05/44)"
                match = re.search(r'\(([A-Z]+)\s+[\d.]+\s+\d+/\d+/\d+\s+-\s+(.+)\)', description)
                if match:
                    symbol = match.group(1)
                    bond_name = match.group(2).strip()
                else:
                    # Fallback: try to extract just the symbol
                    symbol_match = re.search(r'\(([A-Z]+)', description)
                    symbol = symbol_match.group(1) if symbol_match else "UNKNOWN"
                    bond_name = description

                bond_interest = BondInterest(
                    symbol=symbol,
                    payment_date=payment_date,
                    amount=amount,
                    description=description,
                    currency=item.get("Currency", "USD"),
                    bond_name=bond_name,
                )
                interest_payments.append(bond_interest)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse bond interest: {e}")

        return interest_payments

    def _parse_deposits_withdrawals(self) -> List[CashTransaction]:
        """Parse deposits and withdrawals from Deposits & Withdrawals section."""
        transactions = []
        deposits_data = self.sections.get("Deposits & Withdrawals", [])

        print(f"DEBUG: Deposits & Withdrawals section has {len(deposits_data)} items")
        if deposits_data:
            print(f"DEBUG: First item keys: {list(deposits_data[0].keys()) if deposits_data else 'N/A'}")
            for i, item in enumerate(deposits_data[:3]):
                print(f"DEBUG: Item {i}: {item}")

        for item in deposits_data:
            try:
                # Skip header/total rows
                if item.get("Header", "") == "Header":
                    continue

                # Parse date
                date_str = item.get("Settle Date", "") or item.get("Date", "")
                if not date_str:
                    continue
                transaction_date = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()

                # Parse amount
                amount = self._parse_float(item.get("Amount", "0"))
                if amount == 0:
                    continue

                description = item.get("Description", "")
                currency = item.get("Currency", "USD")

                transaction = CashTransaction(
                    transaction_date=transaction_date,
                    amount=amount,
                    description=description,
                    currency=currency,
                )
                transactions.append(transaction)
            except (ValueError, KeyError) as e:
                print(f"Warning: Could not parse deposit/withdrawal: {e}")

        return transactions

    def _parse_trade_datetime(self, date_str: str) -> datetime:
        """Parse trade datetime like '2025-12-01, 10:35:16'."""
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d, %H:%M:%S")
        except ValueError:
            try:
                return datetime.strptime(date_str.strip(), "%Y-%m-%d")
            except ValueError:
                return datetime.now()

    def _parse_trade_action(self, code: str) -> Optional[TradeAction]:
        """Parse trade action from code field."""
        if not code:
            return None
        codes = code.split(";")
        for c in codes:
            c = c.strip()
            if c == "O":
                return TradeAction.OPEN
            elif c == "C":
                return TradeAction.CLOSE
            elif c == "A":
                return TradeAction.ASSIGNED
            elif c == "Ep":
                return TradeAction.EXPIRED
        return None

    @staticmethod
    def _parse_float(value: str) -> float:
        """Parse a string to float, handling various formats."""
        if not value or value == "--":
            return 0.0
        # Remove commas and handle negative values
        cleaned = value.replace(",", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_int(value: str) -> int:
        """Parse a string to int."""
        if not value or value == "--":
            return 0
        cleaned = value.replace(",", "").strip()
        try:
            return int(float(cleaned))
        except ValueError:
            return 0

