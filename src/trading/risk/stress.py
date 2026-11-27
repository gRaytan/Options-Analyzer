"""Stress test and risk budget checks (plan T19, spec §6.2, R8, R9).

Stocks, options and the tail hedge are revalued **together** under market
shocks, because the account is fully invested (R8): in a crash the stocks
fall *and* the short puts lose money, and both shrink Excess Liquidity.

* Each name moves by ``market move x beta``, with beta floored at 1.0 on the
  way down (a stock is assumed to fall at least as much as the market).
* Options are repriced with Black-Scholes at the shocked price and IV
  (``IV x (1 + iv_shock)``), instantly (no time decay). European pricing of
  American options is close enough for a stress estimate.
* Margin after a shock is IB's current maintenance margin scaled by a Reg-T
  model's before/after ratio, then multiplied by ``stress_margin_multiplier``
  (1.5: IB raises requirements in volatile markets).

Checks on an opening trade (the account **after** the trade):

1. ``stress_budget``: options-book loss (excluding stock P&L) in the budget
   scenario (-20%, IV +50%) <= risk budget (10% NLV x regime multiplier).
2. ``stress_el``: account EL in the crash scenario (``risk.crash_move``, default
   S&P -30%, each name x its beta; IV +50%, margin x1.5) stays >= $100k.
3. ``single_name_gap``: for each stock with short puts, a -35% gap on that
   name alone loses <= 2% NLV on its options.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Sequence

from py_vollib.black_scholes import black_scholes

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    OptionContract,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.risk.checks import CheckResult, GateDecision
from src.trading.risk.limits import (
    Exposure,
    apply_intent,
    fully_covered_calls,
    pair_put_spreads,
)
from src.trading.risk.margin_type import reg_t_short_option_margin

RATE = 0.04
DEFAULT_IV = 0.30  # used when an option has no IV; shocked like any other
STOCK_MAINT = 0.25  # Reg-T maintenance on long stock
MIN_T = 1 / 365


@dataclass(frozen=True)
class Scenario:
    name: str
    market_move: float  # e.g. -0.20
    iv_shock: float = 0.0  # relative, e.g. +0.50
    margin_multiplier: float = 1.0


def crash_scenario(config: TradingConfig) -> Scenario:
    """The crash the account must survive: S&P ``crash_move``, IV +50%, margin x1.5."""
    move = config.risk.crash_move
    return Scenario(
        f"crash {move:+.0%} IV+50%", move, 0.50, config.risk.stress_margin_multiplier
    )


def betas_from(config: TradingConfig) -> Dict[str, float]:
    return {s.upper(): e.beta for s, e in config.watchlist.items() if e.beta}


def standard_scenarios(config: TradingConfig) -> List[Scenario]:
    mm = config.risk.stress_margin_multiplier
    out = []
    crash = config.risk.crash_move
    moves = sorted({crash, -0.25, -0.20, -0.15, -0.08, 0.08, 0.15, 0.25})
    for move in moves:
        down = move < 0
        out.append(Scenario(f"{move:+.0%}", move, 0.0, mm if down else 1.0))
    for move in sorted({-0.15, -0.20, -0.25, crash}, reverse=True):
        out.append(Scenario(f"{move:+.0%} IV+50%", move, 0.50, mm))
    return out


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    stock_pnl: float
    options_pnl: float
    maint_margin_after: float
    excess_liquidity_after: float

    @property
    def total_pnl(self) -> float:
        return self.stock_pnl + self.options_pnl


def _option_value(c: OptionContract, und: float, iv: float, today: date) -> float:
    t = max((c.expiry - today).days / 365.0, MIN_T)
    flag = "c" if c.right is Right.CALL else "p"
    # float(): py_vollib returns numpy scalars, which don't serialize to JSON.
    return float(black_scholes(flag, max(und, 0.01), c.strike, t, RATE, max(iv, 0.01)))


def _reg_t_model_margin(
    exposures: Sequence[Exposure], prices: Dict[str, float], values: Dict[str, float]
) -> float:
    """Reg-T-style maintenance: 25% of long stock + uncovered short options
    (+ the width of each put credit spread)."""
    shares: Dict[str, float] = defaultdict(float)
    total = 0.0
    for e in exposures:
        if isinstance(e.contract, StockContract):
            shares[e.underlying] += e.quantity
            total += STOCK_MAINT * abs(e.quantity) * prices[e.underlying]
    # Put credit spreads: Reg-T margin is the width (the most they can lose).
    uncovered: Dict[str, float] = {}
    for e, pairs, left in pair_put_spreads(exposures):
        c = e.contract
        total += sum((c.strike - lk) * c.multiplier * n for lk, n in pairs)
        uncovered[c.key] = left
    for e in exposures:
        c = e.contract
        if not isinstance(c, OptionContract) or e.quantity >= 0:
            continue
        contracts = uncovered.get(c.key, abs(e.quantity))
        if c.right is Right.CALL:  # covered by shares, contract by contract
            covered = min(contracts, max(shares[e.underlying], 0) // c.multiplier)
            shares[e.underlying] -= covered * c.multiplier
            contracts -= covered
        if contracts:
            total += contracts * reg_t_short_option_margin(
                c, prices[e.underlying], values[c.key]
            )
    return total


class StressModel:
    def __init__(
        self,
        exposures: Sequence[Exposure],
        quotes: Dict[str, Quote],
        account: AccountSnapshot,
        today: date,
        betas: Optional[Dict[str, float]] = None,
    ):
        self.exposures = list(exposures)
        self.quotes = quotes
        self.account = account
        self.today = today
        self.betas = betas or {}
        self.base_prices = {e.underlying: e.underlying_price for e in self.exposures}
        self.ivs: Dict[str, float] = {}
        self.base_values: Dict[str, float] = {}
        for e in self.exposures:
            c = e.contract
            if isinstance(c, OptionContract):
                q = quotes.get(c.key)
                iv = q.greeks.iv if q and q.greeks and q.greeks.iv else DEFAULT_IV
                self.ivs[c.key] = iv
                self.base_values[c.key] = _option_value(
                    c, e.underlying_price, iv, today
                )

    def _beta(self, underlying: str, move: float) -> float:
        beta = self.betas.get(underlying.upper(), 1.0)
        return max(beta, 1.0) if move < 0 else beta

    def _shocked(self, move_for) -> Dict[str, float]:
        prices = {u: p * (1 + move_for(u)) for u, p in self.base_prices.items()}
        return prices

    def run(self, scenario: Scenario, only: Optional[str] = None) -> ScenarioResult:
        """Revalue everything; ``only`` shocks a single underlying (a gap)."""

        def move_for(u: str) -> float:
            if only is not None:
                return scenario.market_move if u == only else 0.0
            return scenario.market_move * self._beta(u, scenario.market_move)

        prices = self._shocked(move_for)
        stock_pnl = options_pnl = 0.0
        values: Dict[str, float] = {}
        for e in self.exposures:
            c = e.contract
            if isinstance(c, StockContract):
                stock_pnl += e.quantity * (prices[e.underlying] - e.underlying_price)
                continue
            iv = self.ivs[c.key] * (1 + scenario.iv_shock)
            values[c.key] = _option_value(c, prices[e.underlying], iv, self.today)
            options_pnl += (
                e.quantity * c.multiplier * (values[c.key] - self.base_values[c.key])
            )

        model_before = _reg_t_model_margin(
            self.exposures, self.base_prices, self.base_values
        )
        model_after = _reg_t_model_margin(self.exposures, prices, values)
        ratio = model_after / model_before if model_before > 0 else 1.0
        maint_after = self.account.maint_margin * ratio * scenario.margin_multiplier
        equity_after = (
            self.account.excess_liquidity
            + self.account.maint_margin
            + stock_pnl
            + options_pnl
        )
        return ScenarioResult(
            scenario, stock_pnl, options_pnl, maint_after, equity_after - maint_after
        )


def stress_checks(
    intent: Optional[TradeIntent],
    exposures: Sequence[Exposure],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    regime_multiplier: float = 1.0,
    betas: Optional[Dict[str, float]] = None,
) -> GateDecision:
    """§6.2 checks on the portfolio after ``intent`` (or as is, if None)."""
    if intent is not None and intent.risk_reducing:
        return GateDecision(
            (CheckResult("stress", True, "risk-reducing: stress checks skipped"),)
        )
    if intent is not None and fully_covered_calls(intent, exposures):
        # Shares cover every call: a crash makes the short calls *gain*, and a
        # rally is capped by the shares. The trade can't raise any crash loss,
        # so the account's existing book (which may fail these tests during a
        # recovery) must not block it.
        return GateDecision(
            (CheckResult("stress", True, "covered call: can't add crash loss"),)
        )
    after = (
        apply_intent(list(exposures), intent, quotes)
        if intent is not None
        else list(exposures)
    )
    if betas is None:
        betas = {s: e.beta for s, e in config.watchlist.items() if e.beta}
    model = StressModel(after, quotes, account, today, betas)
    nlv = account.net_liquidation
    risk, floor = config.risk, config.margin_floor
    mm = risk.stress_margin_multiplier
    checks: List[CheckResult] = []

    budget_scn = Scenario("budget -20% IV+50%", -0.20, 0.50, mm)
    budget = risk.risk_budget_pct_nlv * regime_multiplier * nlv
    loss = -model.run(budget_scn).options_pnl
    checks.append(
        CheckResult(
            "stress_budget",
            loss <= budget + 1e-6,
            f"options loss ${loss:,.0f} in -20%/IV+50% (budget ${budget:,.0f})",
        )
    )

    worst = model.run(crash_scenario(config))
    checks.append(
        CheckResult(
            "stress_el",
            worst.excess_liquidity_after >= floor.excess_liquidity_min,
            f"EL ${worst.excess_liquidity_after:,.0f} in {risk.crash_move:+.0%}/IV+50%/margin x{mm:g} "
            f"(floor ${floor.excess_liquidity_min:,.0f})",
        )
    )

    gap_limit = risk.single_name_gap_max_loss * nlv
    gap_problems = []
    names = {
        e.underlying
        for e in after
        if isinstance(e.contract, OptionContract) and e.is_short_put and not e.is_index
    }
    for name in sorted(names):
        gap = model.run(
            Scenario(f"{name} gap", -risk.single_name_gap, 0.50, 1.0), only=name
        )
        if -gap.options_pnl > gap_limit:
            gap_problems.append(f"{name}: ${-gap.options_pnl:,.0f}")
    checks.append(
        CheckResult(
            "single_name_gap",
            not gap_problems,
            "; ".join(gap_problems) + f" > ${gap_limit:,.0f}" if gap_problems else "ok",
        )
    )
    return GateDecision(tuple(checks))
