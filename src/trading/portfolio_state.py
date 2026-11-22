"""Live IB data -> the existing Portfolio model (plan T08, spec G5).

The analysis code (``RiskAnalyzer``, ``MarginCalculator``, the web pages)
works on ``src.models.portfolio.Portfolio``. This module builds that same
object from broker positions and the account snapshot, so the existing
analysis runs unchanged on live data instead of a CSV export.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from src.models.portfolio import Portfolio
from src.models.position import OptionPosition, OptionType, StockPosition
from src.trading.domain import (
    AccountSnapshot,
    OptionContract,
    Position,
    Right,
    StockContract,
)


def legacy_option_symbol(contract: OptionContract) -> str:
    """The CSV parser's symbol format, e.g. ``AAPL 15JAN27 110 P``."""
    root = contract.trading_class or contract.underlying
    expiry = contract.expiry.strftime("%d%b%y").upper()
    return f"{root} {expiry} {contract.strike:g} {contract.right.value}"


def build_portfolio(
    positions: Iterable[Position],
    account: Optional[AccountSnapshot] = None,
    underlying_prices: Optional[Dict[str, float]] = None,
) -> Portfolio:
    """Build a ``Portfolio`` like the CSV parser would.

    ``underlying_prices`` fills ``OptionPosition.underlying_price``; stock
    positions' market prices are used when no price is given for a symbol.
    Missing market prices fall back to the average cost.
    """
    positions = list(positions)
    prices: Dict[str, float] = {}
    for pos in positions:
        if isinstance(pos.contract, StockContract) and pos.market_price is not None:
            prices[pos.contract.symbol] = pos.market_price
    prices.update(underlying_prices or {})

    portfolio = Portfolio()
    for pos in positions:
        current = pos.market_price if pos.market_price is not None else pos.avg_cost
        contract = pos.contract
        if isinstance(contract, StockContract):
            portfolio.stocks.append(
                StockPosition(
                    symbol=contract.symbol,
                    quantity=int(pos.quantity),
                    cost_price=pos.avg_cost,
                    current_price=current,
                    currency=contract.currency,
                )
            )
        elif isinstance(contract, OptionContract):
            portfolio.options.append(
                OptionPosition(
                    symbol=legacy_option_symbol(contract),
                    underlying=contract.underlying,
                    strike=contract.strike,
                    expiration=contract.expiry,
                    option_type=(
                        OptionType.CALL
                        if contract.right is Right.CALL
                        else OptionType.PUT
                    ),
                    quantity=int(pos.quantity),
                    cost_price=pos.avg_cost,
                    current_price=current,
                    currency=contract.currency,
                    underlying_price=prices.get(contract.underlying, 0.0),
                )
            )

    if account is not None:
        portfolio.account_id = account.account_id
        portfolio.net_asset_value = account.net_liquidation
        portfolio.net_liquidation_value = account.net_liquidation
        portfolio.excess_liquidity = account.excess_liquidity
        portfolio.maintenance_margin = account.maint_margin
        portfolio.initial_margin = account.init_margin
        portfolio.available_funds = account.available_funds
        portfolio.cash_balance = account.cash
    return portfolio
