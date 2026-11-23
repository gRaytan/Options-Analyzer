"""Data storage for positions and portfolio data."""
import json
import os
from datetime import date, datetime
from typing import Optional
from pathlib import Path

from src.models.portfolio import Portfolio
from src.models.position import OptionPosition, StockPosition, OptionType


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that handles date and datetime objects."""

    def default(self, obj):
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        return super().default(obj)


class DataStorage:
    """File-based storage for portfolio data."""

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.positions_dir = self.data_dir / "positions"
        self.config_dir = self.data_dir / "config"
        self.uploads_dir = self.data_dir / "uploads"

        # Ensure directories exist
        self.positions_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    def save_portfolio(
        self, portfolio: Portfolio, filename: str = "portfolio.json"
    ) -> str:
        """Save portfolio to JSON file."""
        filepath = self.positions_dir / filename

        with open(filepath, "w") as f:
            json.dump(portfolio.to_dict(), f, indent=2, cls=DateTimeEncoder)

        return str(filepath)

    def load_portfolio(self, filename: str = "portfolio.json") -> Optional[Portfolio]:
        """Load portfolio from JSON file."""
        filepath = self.positions_dir / filename

        if not filepath.exists():
            return None

        with open(filepath, "r") as f:
            data = json.load(f)

        return self._dict_to_portfolio(data)

    def _dict_to_portfolio(self, data: dict) -> Portfolio:
        """Convert dictionary back to Portfolio object."""
        portfolio = Portfolio(
            account_id=data.get("account_id", ""),
            account_name=data.get("account_name", ""),
            base_currency=data.get("base_currency", "USD"),
            statement_period=data.get("statement_period", ""),
            net_asset_value=data.get("net_asset_value", 0.0),
            cash_balance=data.get("cash_balance", 0.0),
        )

        # Reconstruct options
        for opt_data in data.get("options", []):
            option = OptionPosition(
                symbol=opt_data.get("symbol", ""),
                underlying=opt_data.get("underlying", ""),
                strike=opt_data.get("strike", 0.0),
                expiration=self._parse_date(opt_data.get("expiration")),
                option_type=OptionType(opt_data["option_type"])
                if opt_data.get("option_type")
                else None,
                quantity=opt_data.get("quantity", 0),
                cost_price=opt_data.get("cost_price", 0.0),
                current_price=opt_data.get("current_price", 0.0),
                underlying_price=opt_data.get("underlying_price", 0.0),
                currency=opt_data.get("currency", "USD"),
                delta=opt_data.get("delta", 0.0),
                gamma=opt_data.get("gamma", 0.0),
                theta=opt_data.get("theta", 0.0),
                vega=opt_data.get("vega", 0.0),
                implied_volatility=opt_data.get("implied_volatility", 0.0),
                margin_requirement=opt_data.get("margin_requirement", 0.0),
                flags=opt_data.get("flags", []),
            )
            portfolio.options.append(option)

        # Reconstruct stocks
        for stock_data in data.get("stocks", []):
            stock = StockPosition(
                symbol=stock_data.get("symbol", ""),
                quantity=stock_data.get("quantity", 0),
                cost_price=stock_data.get("cost_price", 0.0)
                if "cost_price" in stock_data
                else 0.0,
                current_price=stock_data.get("current_price", 0.0),
                currency=stock_data.get("currency", "USD"),
            )
            portfolio.stocks.append(stock)

        return portfolio

    @staticmethod
    def _parse_date(date_str: Optional[str]) -> Optional[date]:
        """Parse ISO date string to date object."""
        if not date_str:
            return None
        try:
            return datetime.fromisoformat(date_str).date()
        except ValueError:
            return None

    def save_uploaded_file(self, content: bytes, filename: str) -> str:
        """Save an uploaded file."""
        filepath = self.uploads_dir / filename
        with open(filepath, "wb") as f:
            f.write(content)
        return str(filepath)

    def get_config(self, filename: str = "settings.json") -> dict:
        """Load configuration file."""
        filepath = self.config_dir / filename

        if not filepath.exists():
            return self._default_config()

        with open(filepath, "r") as f:
            return json.load(f)

    def save_config(self, config: dict, filename: str = "settings.json") -> None:
        """Save configuration file."""
        filepath = self.config_dir / filename
        with open(filepath, "w") as f:
            json.dump(config, f, indent=2)

    @staticmethod
    def _default_config() -> dict:
        """Return default configuration."""
        return {
            "profit_target_percentage": 50,
            "high_delta_threshold": 0.6,
            "margin_warning_level": 0.8,
            "days_to_expiration_warning": 7,
            "risk_free_rate": 0.05,
        }
