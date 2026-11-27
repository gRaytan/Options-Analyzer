"""Hard risk limits for opening trades (plan T18a, spec §6.1, R1, R8, R9).

Every **opening** intent (new trades and rolls) must pass all of these.
Risk-reducing intents (closes, margin restoration) skip them so the bot can
always get out; they still pass the sanity checks (T17).

| Check | Rule (defaults) |
|---|---|
| ``el_after`` | IB what-if Excess Liquidity after the trade >= $125k |
| ``cushion_after`` | EL after / net liquidation >= 40% |
| ``short_put_notional`` | all short puts: strike x 100 x qty <= 50% NLV (a put spread counts at its width) |
| ``index_put_notional`` | index short puts (XSP/SPX...) <= 40% NLV |
| ``expiry_ladder`` | short-put notional in one expiry <= 35% of the short-put cap |
| ``per_name`` | delta-dollar exposure per underlying <= 15% NLV, or not increased |
| ``per_sector`` | same per sector, 35% NLV |
| ``options_beta_delta`` | options-only beta-weighted delta-dollars <= +35% NLV |
| ``contracts_per_order`` | <= 10 (SPX/SPXW 5, XSP 20) |
| ``orders_per_day`` | opening orders today < 5 |

**Exposure** is delta-dollars: shares x price for stock, and
``delta x 100 x qty x underlying price`` for options. A short put adds
long exposure; a covered call removes some. Per-name and per-sector caps
block trades that **increase** exposure above the cap; your existing
holdings may already exceed 15%, and the bot never sells them (R8).

The expiry ladder is measured against the short-put **cap** (50% NLV x 35%
= 17.5% NLV per expiry), not against the current total. Against the current
total, the very first put would always be 100% of the total and blocked.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.trading.config import RiskConfig, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    Contract,
    MarginImpact,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.risk.checks import CheckResult, GateDecision

INDEX_ROOTS = {"SPX", "SPXW", "XSP", "NDX", "XND", "RUT", "MRUT", "DJX"}
SPX_ROOTS = {"SPX", "SPXW"}
EPS = 1e-6


@dataclass(frozen=True)
class Exposure:
    """One position's risk inputs: signed quantity plus delta and prices."""

    contract: Contract
    quantity: float
    delta: float  # per share (stock = 1.0)
    underlying_price: float

    @property
    def underlying(self) -> str:
        c = self.contract
        return c.underlying if isinstance(c, OptionContract) else c.symbol

    @property
    def multiplier(self) -> int:
        return self.contract.multiplier

    @property
    def delta_dollars(self) -> float:
        return self.delta * self.multiplier * self.quantity * self.underlying_price

    @property
    def is_short_put(self) -> bool:
        c = self.contract
        return (
            isinstance(c, OptionContract) and c.right is Right.PUT and self.quantity < 0
        )

    @property
    def put_notional(self) -> float:
        return (
            self.contract.strike * self.multiplier * abs(self.quantity)
            if self.is_short_put
            else 0.0
        )

    @property
    def is_index(self) -> bool:
        c = self.contract
        root = (getattr(c, "trading_class", None) or self.underlying).upper()
        return root in INDEX_ROOTS or self.underlying.upper() in INDEX_ROOTS


def pair_put_spreads(
    exposures: Iterable[Exposure],
) -> List[Tuple[Exposure, List[Tuple[float, float]], float]]:
    """Each short put with the long puts that cap it: (short, [(long strike, qty)], uncovered qty).

    A long put on the same root and expiry at a lower strike turns a short put
    into a put credit spread (spec §5.4) that can lose at most the width.
    Longs pair with the highest short strikes first, highest long first.
    """
    groups: Dict[tuple, Dict[str, list]] = defaultdict(lambda: {"s": [], "l": []})
    for e in exposures:
        c = e.contract
        if not isinstance(c, OptionContract) or c.right is not Right.PUT:
            continue
        key = ((c.trading_class or c.underlying).upper(), c.expiry, c.multiplier)
        if e.quantity < 0:
            groups[key]["s"].append(e)
        elif e.quantity > 0:
            groups[key]["l"].append([c.strike, float(e.quantity)])
    out = []
    for g in groups.values():
        longs = sorted(g["l"], key=lambda x: -x[0])
        for e in sorted(g["s"], key=lambda x: -x.contract.strike):
            k, left, pairs = e.contract.strike, abs(e.quantity), []
            for lg in longs:
                if left <= EPS:
                    break
                if lg[0] >= k or lg[1] <= EPS:
                    continue
                take = min(left, lg[1])
                pairs.append((lg[0], take))
                lg[1] -= take
                left -= take
            out.append((e, pairs, left))
    return out


def net_put_notional(exposures: Iterable[Exposure]) -> List[Tuple[Exposure, float]]:
    """Each short put's notional, net of long puts that cap its loss: a spread
    counts as ``width x multiplier`` per pair, an unpaired short at its strike."""
    out = []
    for e, pairs, left in pair_put_spreads(exposures):
        k, mult = e.contract.strike, e.multiplier
        notional = sum((k - lk) * mult * n for lk, n in pairs) + k * mult * left
        out.append((e, notional))
    return out


def worst_case_delta(contract: OptionContract, quantity: float) -> float:
    """Delta to assume when an option has no quote.

    The caps here limit **long** market exposure (a crash hurts it), so an
    unknown delta takes the value that adds the most long exposure:
    long calls +1, short puts -1 (short x -1 = long), and 0 for short calls
    and long puts, whose hedging effect isn't counted without a quote.
    """
    if contract.right is Right.CALL:
        return 1.0 if quantity > 0 else 0.0
    return -1.0 if quantity < 0 else 0.0


def exposures_from(
    positions: Iterable[Position], quotes: Dict[str, Quote]
) -> List[Exposure]:
    """Build exposures from positions and quotes (deltas, underlying prices).

    Stocks use their market price. Options need a quote with Greeks; an option
    without a delta is counted at delta 1 (worst case) so it can't hide risk.
    """
    positions = list(positions)
    stock_prices: Dict[str, float] = {}
    for pos in positions:
        if isinstance(pos.contract, StockContract) and pos.market_price:
            stock_prices[pos.contract.symbol] = pos.market_price
    out = []
    for pos in positions:
        c = pos.contract
        if isinstance(c, StockContract):
            out.append(Exposure(c, pos.quantity, 1.0, pos.market_price or pos.avg_cost))
            continue
        q = quotes.get(c.key)
        if q and q.greeks:
            delta = q.greeks.delta
        else:
            delta = worst_case_delta(c, pos.quantity)
        und = (
            q.underlying_price if q and q.underlying_price else None
        ) or stock_prices.get(c.underlying, c.strike)
        out.append(Exposure(c, pos.quantity, delta, und))
    return out


def apply_intent(
    exposures: List[Exposure], intent: TradeIntent, quotes: Dict[str, Quote]
) -> List[Exposure]:
    """Exposures after the intent's legs fill."""
    after = list(exposures)
    by_key = {e.contract.key: i for i, e in enumerate(after)}
    for leg in intent.legs:
        change = leg.action.sign * leg.quantity
        key = leg.contract.key
        if key in by_key:
            old = after[by_key[key]]
            after[by_key[key]] = Exposure(
                old.contract, old.quantity + change, old.delta, old.underlying_price
            )
        else:
            price = None
            q = quotes.get(key)
            if q is not None:
                price = q.mid if q.mid is not None else q.last
            [new] = exposures_from(
                [Position(leg.contract, change, price or 0.0, price)], quotes
            )
            by_key[key] = len(after)
            after.append(new)
    return after


@dataclass
class LimitContext:
    account: AccountSnapshot
    impact: MarginImpact  # IB what-if for this order
    exposures: List[Exposure]  # before the trade
    quotes: Dict[str, Quote]
    opening_orders_today: int = 0
    sectors: Dict[str, str] = field(default_factory=dict)
    betas: Dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: TradingConfig, **kw) -> "LimitContext":
        sectors = {s: e.sector for s, e in config.watchlist.items() if e.sector}
        betas = {s: e.beta for s, e in config.watchlist.items() if e.beta}
        return cls(sectors=sectors, betas=betas, **kw)


def _group_dollars(exposures: Iterable[Exposure], key) -> Dict[str, float]:
    totals: Dict[str, float] = defaultdict(float)
    for e in exposures:
        totals[key(e)] += e.delta_dollars
    return totals


def _cap_check(name, before, after, cap_dollars, label) -> CheckResult:
    over = {
        k: v
        for k, v in after.items()
        if v > cap_dollars + EPS and v > before.get(k, 0.0) + EPS
    }
    if over:
        detail = "; ".join(
            f"{k}: ${v:,.0f} > cap ${cap_dollars:,.0f}" for k, v in sorted(over.items())
        )
        return CheckResult(
            name, False, f"{label} exposure would rise above the cap ({detail})"
        )
    return CheckResult(name, True)


def _concentration_checks(
    ctx, after, risk: RiskConfig, nlv: float
) -> List[CheckResult]:
    """Per-name and per-sector delta-dollar caps; block increases above the cap."""

    def name_of(e: Exposure) -> str:
        return "SPX" if e.is_index else e.underlying.upper()

    def sector_of(e: Exposure) -> str:
        return "index" if e.is_index else ctx.sectors.get(name_of(e), name_of(e))

    return [
        _cap_check(
            "per_name",
            _group_dollars(ctx.exposures, name_of),
            _group_dollars(after, name_of),
            risk.per_name_max * nlv,
            "per-name",
        ),
        _cap_check(
            "per_sector",
            _group_dollars(ctx.exposures, sector_of),
            _group_dollars(after, sector_of),
            risk.per_sector_max * nlv,
            "per-sector",
        ),
    ]


def fully_covered_calls(intent: TradeIntent, exposures: Sequence[Exposure]) -> bool:
    """True when every leg sells calls that shares already held fully cover.

    Shares per underlying must cover the calls already short there plus the
    new ones, 100 shares (the multiplier) per contract.
    """
    if not intent.legs:
        return False
    new: Dict[str, int] = defaultdict(int)
    for leg in intent.legs:
        c = leg.contract
        if not (
            isinstance(c, OptionContract)
            and c.right is Right.CALL
            and leg.action is Action.SELL
        ):
            return False
        new[c.underlying.upper()] += leg.quantity * c.multiplier
    for sym, need in new.items():
        shares = sum(
            e.quantity
            for e in exposures
            if isinstance(e.contract, StockContract) and e.underlying.upper() == sym
        )
        short_calls = sum(
            -e.quantity * e.multiplier
            for e in exposures
            if isinstance(e.contract, OptionContract)
            and e.contract.right is Right.CALL
            and e.quantity < 0
            and e.underlying.upper() == sym
        )
        if shares < short_calls + need:
            return False
    return True


def _margin_after_checks(ctx, floor, nlv: float) -> List[CheckResult]:
    el_after = ctx.impact.excess_liquidity_after
    need = floor.entry_min_excess_liquidity
    cushion_after = el_after / nlv if nlv > 0 else 0.0
    return [
        CheckResult(
            "el_after",
            el_after >= need,
            f"EL after ${el_after:,.0f} (need ${need:,.0f})",
        ),
        CheckResult(
            "cushion_after",
            cushion_after >= floor.cushion_min_new_trade,
            f"cushion after {cushion_after:.1%} (need {floor.cushion_min_new_trade:.0%})",
        ),
    ]


def check_limits(
    intent: TradeIntent, ctx: LimitContext, floor, risk: RiskConfig
) -> GateDecision:
    """All §6.1 limits for one intent. Risk-reducing intents pass automatically."""
    if intent.risk_reducing:
        return GateDecision(
            (CheckResult("limits", True, "risk-reducing: allocation limits skipped"),)
        )

    nlv = ctx.account.net_liquidation
    checks: List[CheckResult] = []

    # R1 / R9: margin after the trade, from IB's what-if. A fully covered call
    # needs no margin and adds cash, so it can't lower Excess Liquidity: it's
    # allowed even when the account is under the new-trade floor.
    if fully_covered_calls(intent, ctx.exposures):
        checks.append(CheckResult("el_after", True, "covered call: needs no margin"))
        checks.append(
            CheckResult("cushion_after", True, "covered call: needs no margin")
        )
    else:
        checks.extend(_margin_after_checks(ctx, floor, nlv))

    after = apply_intent(ctx.exposures, intent, ctx.quotes)

    # R8: short-put notional caps (spreads count at their width).
    netted = net_put_notional(after)
    put_total = sum(n for _, n in netted)
    put_cap = risk.short_put_notional_max * nlv
    checks.append(
        CheckResult(
            "short_put_notional",
            put_total <= put_cap + EPS,
            f"short puts ${put_total:,.0f} (cap ${put_cap:,.0f})",
        )
    )
    index_total = sum(n for e, n in netted if e.is_index)
    index_cap = risk.index_put_notional_max * nlv
    checks.append(
        CheckResult(
            "index_put_notional",
            index_total <= index_cap + EPS,
            f"index short puts ${index_total:,.0f} (cap ${index_cap:,.0f})",
        )
    )

    # Expiry ladder, measured against the short-put cap.
    by_expiry: Dict[date, float] = defaultdict(float)
    for e, n in netted:
        by_expiry[e.contract.expiry] += n
    ladder_cap = risk.expiry_max_share * put_cap
    worst = max(by_expiry.items(), key=lambda kv: kv[1], default=(None, 0.0))
    checks.append(
        CheckResult(
            "expiry_ladder",
            worst[1] <= ladder_cap + EPS,
            f"largest expiry {worst[0]}: ${worst[1]:,.0f} (cap ${ladder_cap:,.0f})",
        )
    )

    restores = bool(intent.details.get("restore_assignment"))
    # Rebuilding the owner's holdings after a margin call (rebuild.py): shares
    # the owner approved one by one. Sized by margin and crash stress, which
    # still apply; per-name, contract-count and daily-order caps are for options.
    rebuild_shares = bool(intent.details.get("rebuild")) and all(
        isinstance(leg.contract, StockContract) for leg in intent.legs
    )

    # Concentration: per name and per sector (delta-dollars), block increases above cap.
    # A rebuy after a call assignment restores the previous holding: not a new
    # concentration (T28c), so it skips these caps; margin checks above still apply.
    if restores:
        checks.append(CheckResult("per_name", True, "restores pre-assignment shares"))
        checks.append(CheckResult("per_sector", True, "restores pre-assignment shares"))
    elif rebuild_shares:
        checks.append(
            CheckResult("per_name", True, "owner-approved rebuild of holdings")
        )
        checks.append(
            CheckResult("per_sector", True, "owner-approved rebuild of holdings")
        )
    else:
        checks.extend(_concentration_checks(ctx, after, risk, nlv))

    # Options-only beta-weighted delta (stocks excluded: you're fully invested by design).
    def options_beta_dollars(items):
        return sum(
            e.delta_dollars * ctx.betas.get(e.underlying.upper(), 1.0)
            for e in items
            if isinstance(e.contract, OptionContract)
        )

    beta_before, beta_after = options_beta_dollars(ctx.exposures), options_beta_dollars(
        after
    )
    beta_cap = risk.options_beta_delta_max * nlv
    checks.append(
        CheckResult(
            "options_beta_delta",
            beta_after <= beta_cap + EPS or beta_after <= beta_before + EPS,
            f"options beta-delta ${beta_after:,.0f} (cap ${beta_cap:,.0f})",
        )
    )

    # Order size and daily order count.
    max_qty = max(leg.quantity for leg in intent.legs)
    roots = {
        (
            getattr(leg.contract, "trading_class", None)
            or getattr(leg.contract, "underlying", "")
        ).upper()
        for leg in intent.legs
    }
    cap = risk.max_contracts_per_order
    if roots & SPX_ROOTS:
        cap = risk.max_contracts_spx
    elif "XSP" in roots:
        cap = risk.max_contracts_xsp
    if rebuild_shares:
        checks.append(CheckResult("contracts_per_order", True, f"{max_qty} shares"))
        checks.append(CheckResult("orders_per_day", True, "owner-approved rebuild"))
        return GateDecision(tuple(checks))
    checks.append(
        CheckResult(
            "contracts_per_order", max_qty <= cap, f"{max_qty} contracts (cap {cap})"
        )
    )
    checks.append(
        CheckResult(
            "orders_per_day",
            ctx.opening_orders_today < risk.max_orders_per_day,
            f"{ctx.opening_orders_today} opening orders today (cap {risk.max_orders_per_day})",
        )
    )
    return GateDecision(tuple(checks))


def gate_decision(
    intent: TradeIntent, ctx: LimitContext, config: TradingConfig
) -> GateDecision:
    return check_limits(intent, ctx, config.margin_floor, config.risk)
