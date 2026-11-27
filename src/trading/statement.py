"""Crash check from an uploaded IB statement (no IB connection needed).

The web app already parses IB Activity Statements (positions, prices, Net
Liquidation, Excess Liquidity, maintenance margin). This turns that
:class:`~src.models.portfolio.Portfolio` into the bot's own model, so the same
code that guards live trading can answer, from the statement alone:

* what the account looks like if the S&P 500 falls 10%, 20% or 30% (each
  stock by its beta, at least 1x down; IV +50%; IB margin up to x1.5, in
  proportion to the drop), and whether
  IB would issue a margin call (Excess Liquidity under $100k);
* what each position contributes to that loss;
* the rebuild plan's next step, as a preview.

Prices are the statement's closing prices, so results are as of the
statement date. A stock you no longer hold (e.g. sold in a margin call)
takes its price from your last trade in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Dict, List, Optional, Tuple

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    ExerciseStyle,
    Greeks,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
    normalize_symbol,
)
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import (
    Scenario,
    StressModel,
    _reg_t_model_margin,
    betas_from,
)

CASH_SETTLED = {"SPX", "SPXW", "XSP", "NDX", "XND", "RUT", "MRUT"}
MOVES = (-0.10, -0.20, -0.30)


@dataclass
class StatementBook:
    as_of: Optional[date]
    account: AccountSnapshot
    positions: List[Position]
    quotes: Dict[str, Quote]
    last_prices: Dict[str, float] = field(default_factory=dict)  # from trades
    notes: List[str] = field(default_factory=list)

    def prices(self) -> Dict[str, float]:
        """Stock prices: held positions first, then last trade prices."""
        out = dict(self.last_prices)
        for p in self.positions:
            if isinstance(p.contract, StockContract) and p.market_price:
                out[p.contract.symbol] = p.market_price
        return out


def _ts(day: Optional[date]) -> datetime:
    return datetime.combine(day or date.today(), time(21, 0), tzinfo=timezone.utc)


def from_portfolio(portfolio) -> StatementBook:
    """Legacy statement Portfolio -> account snapshot, positions and quotes."""
    as_of = portfolio.period_end
    ts = _ts(as_of)
    notes: List[str] = []
    positions: List[Position] = []
    quotes: Dict[str, Quote] = {}
    for s in portfolio.stocks:
        sym = normalize_symbol(s.symbol)
        positions.append(
            Position(
                StockContract(sym), float(s.quantity), s.cost_price, s.current_price
            )
        )
    for o in portfolio.options:
        und = normalize_symbol(o.underlying or o.symbol.split()[0])
        if o.expiration is None or o.option_type is None:
            notes.append(f"{o.symbol}: no expiry or right in the statement, skipped")
            continue
        index = und.replace(" ", "") in CASH_SETTLED
        c = OptionContract(
            und,
            o.expiration,
            float(o.strike),
            Right(o.option_type.value),
            100,
            ExerciseStyle.EUROPEAN if index else ExerciseStyle.AMERICAN,
            index,
            und,
        )
        positions.append(Position(c, float(o.quantity), o.cost_price, o.current_price))
        greeks = None
        if o.implied_volatility:
            iv = (
                o.implied_volatility / 100
                if o.implied_volatility > 3
                else o.implied_volatility
            )
            greeks = Greeks(o.delta or 0.0, iv=iv)
        quotes[c.key] = Quote(
            c,
            o.current_price,
            o.current_price,
            ts,
            last=o.current_price,
            underlying_price=o.underlying_price or None,
            greeks=greeks,
        )

    nlv = portfolio.net_liquidation_value or portfolio.net_asset_value
    maint = portfolio.maintenance_margin
    if not maint:
        maint = _estimate_maint(positions, quotes)
        notes.append(
            "Maintenance margin not in the statement: estimated with Reg-T rules"
        )
    el = portfolio.excess_liquidity or (nlv - maint)
    init = portfolio.initial_margin or maint
    account = AccountSnapshot(
        portfolio.account_id or "statement",
        ts,
        nlv,
        el,
        maint,
        init,
        portfolio.available_funds or (nlv - init),
        portfolio.cash_balance,
    )
    last_prices: Dict[str, float] = {}
    for t in sorted(getattr(portfolio, "trades", []), key=lambda t: t.trade_date):
        if getattr(t, "asset_type", "") == "Stock" and t.price:
            last_prices[normalize_symbol(t.symbol)] = t.price
    if nlv <= 0:
        notes.append(
            "No net liquidation value in the statement: upload a full Activity Statement"
        )
    return StatementBook(as_of, account, positions, quotes, last_prices, notes)


def _estimate_maint(positions, quotes) -> float:
    exposures = exposures_from(positions, quotes)
    prices = {e.underlying: e.underlying_price for e in exposures}
    values = {p.contract.key: p.market_price or 0.0 for p in positions}
    return _reg_t_model_margin(exposures, prices, values)


@dataclass
class CrashRow:
    move: float
    stock_pnl: float
    options_pnl: float
    nlv_after: float
    el_after: float
    margin_call: bool


def margin_multiplier(move: float, config: TradingConfig) -> float:
    """IB's margin increase grows with the size of the drop: the full
    ``stress_margin_multiplier`` (x1.5) at the configured crash, proportionally
    less for smaller drops (x1.17 at -10%). The risk gate always uses the full one."""
    mm = config.risk.stress_margin_multiplier
    share = min(1.0, abs(move) / abs(config.risk.crash_move))
    return 1.0 + (mm - 1.0) * share


def crash_table(book: StatementBook, config: TradingConfig) -> List[CrashRow]:
    """The account in S&P drops of 10/20/30% (and the configured crash)."""
    model = StressModel(
        exposures_from(book.positions, book.quotes),
        book.quotes,
        book.account,
        book.as_of or date.today(),
        betas_from(config),
    )
    floor = config.margin_floor.excess_liquidity_min
    rows = []
    for move in sorted(set(MOVES) | {config.risk.crash_move}, reverse=True):
        r = model.run(
            Scenario(f"{move:+.0%}", move, 0.50, margin_multiplier(move, config))
        )
        rows.append(
            CrashRow(
                move,
                r.stock_pnl,
                r.options_pnl,
                book.account.net_liquidation + r.total_pnl,
                r.excess_liquidity_after,
                r.excess_liquidity_after < floor,
            )
        )
    return rows


def by_position(
    book: StatementBook, config: TradingConfig, move: float
) -> List[Tuple[str, float, float]]:
    """(name, market value, P&L in the crash) per stock / option underlying, worst first."""
    betas = betas_from(config)
    out: Dict[str, List[float]] = {}
    for p in book.positions:
        c = p.contract
        name = c.symbol if isinstance(c, StockContract) else f"{c.underlying} options"
        model = StressModel(
            exposures_from([p], book.quotes),
            book.quotes,
            book.account,
            book.as_of or date.today(),
            betas,
        )
        r = model.run(Scenario("one", move, 0.50, 1.0))
        value = p.quantity * (p.market_price or 0.0) * c.multiplier
        agg = out.setdefault(name, [0.0, 0.0])
        agg[0] += value
        agg[1] += r.total_pnl
    return sorted(((k, v[0], v[1]) for k, v in out.items()), key=lambda x: x[2])
