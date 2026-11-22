"""Automatic margin restoration (plan T26, spec §6.5, R1, R5, R9).

When the margin monitor (T16) reports **breach** or **emergency**, the bot
closes short options until Excess Liquidity is back to ``restore_target``
($110k) **and** Cushion to ``cushion_restore`` (35%).

Which positions, and in what order (fewest transactions, R2):

1. Positions already flagged for closing (high delta, ITM, pin risk, large
   loss): they're risky anyway.
2. The rest by **EL gained per dollar of closing cost**: the margin freed by
   closing it, minus fees and slippage (half the spread), per dollar of
   fees + slippage. The cheapest margin first.
3. Whole positions only; stop as soon as the planned EL gain covers the need.

Never closed: long options (the tail hedge), stock (R8), and covered calls
(they hold no margin, so closing them frees nothing). Any position in the
account may be closed (owner-confirmed, spec open question 9).

Margin freed per position is estimated with the Reg-T formula (T18b) unless
the caller supplies IB's what-if figure (available from Stage 3).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Set

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.fees import estimate_order_fees
from src.trading.risk.margin_type import reg_t_short_option_margin

log = logging.getLogger("trading.restore")

STRATEGY = "margin_restoration"


@dataclass(frozen=True)
class CloseCandidate:
    position: Position
    margin_freed: float
    cost: float  # fees + slippage (half the spread x contracts x multiplier)
    flagged: bool

    @property
    def el_gain(self) -> float:
        return self.margin_freed - self.cost

    @property
    def score(self) -> float:
        return self.el_gain / max(self.cost, 0.01)


@dataclass
class RestorationPlan:
    need: float  # EL to add to reach both restore targets
    closes: List[CloseCandidate] = field(default_factory=list)
    planned_gain: float = 0.0

    @property
    def sufficient(self) -> bool:
        return self.planned_gain >= self.need


def el_needed(
    account: AccountSnapshot, el_estimate: float, config: TradingConfig
) -> float:
    """EL to add so EL >= restore_target and Cushion >= cushion_restore."""
    floor = config.margin_floor
    nlv = account.net_liquidation
    need_el = floor.restore_target - el_estimate
    # Cushion = EL / NLV; closing at mid leaves NLV about the same.
    need_cushion = floor.cushion_restore * nlv - el_estimate
    return max(0.0, need_el, need_cushion)


def _uncovered_calls(positions: List[Position]) -> Dict[str, float]:
    """Short call contracts not covered by shares, per position key."""
    shares: Dict[str, float] = defaultdict(float)
    for p in positions:
        if isinstance(p.contract, StockContract):
            shares[p.contract.symbol] += p.quantity
    out = {}
    for p in positions:
        c = p.contract
        if isinstance(c, OptionContract) and c.right is Right.CALL and p.quantity < 0:
            covered = min(abs(p.quantity), max(shares[c.underlying], 0) // c.multiplier)
            shares[c.underlying] -= covered * c.multiplier
            out[c.key] = abs(p.quantity) - covered
    return out


def candidates(
    positions: Iterable[Position],
    quotes: Dict[str, Quote],
    config: TradingConfig,
    flagged: Set[str],
    margin_freed: Optional[Dict[str, float]] = None,
) -> List[CloseCandidate]:
    positions = list(positions)
    uncovered = _uncovered_calls(positions)
    prices = {
        p.contract.symbol: p.market_price
        for p in positions
        if isinstance(p.contract, StockContract)
    }
    out = []
    for p in positions:
        c = p.contract
        if not isinstance(c, OptionContract) or p.quantity >= 0:
            continue  # stock and long options (hedge) are never closed here
        n_naked = uncovered.get(c.key, abs(p.quantity))
        if n_naked <= 0:
            continue  # fully covered call: frees no margin
        q = quotes.get(c.key)
        if q is None or q.mid is None:
            continue
        und = q.underlying_price or prices.get(c.underlying) or c.strike
        freed = (margin_freed or {}).get(c.key)
        if freed is None:
            freed = n_naked * reg_t_short_option_margin(c, und, q.mid)
        qty = int(abs(p.quantity))
        probe = OrderRequest(
            "probe", (OrderLeg(c, Action.BUY, qty),), q.ask or q.mid, "probe"
        )
        fees = estimate_order_fees(probe, {c.key: q.mid}, config.fees).total
        slippage = (q.spread or 0.0) / 2 * qty * c.multiplier
        out.append(CloseCandidate(p, freed, fees + slippage, c.key in flagged))
    return out


def plan(
    positions: Iterable[Position],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    el_estimate: float,
    config: TradingConfig,
    flagged: Optional[Set[str]] = None,
    margin_freed: Optional[Dict[str, float]] = None,
) -> RestorationPlan:
    need = el_needed(account, el_estimate, config)
    result = RestorationPlan(need)
    if need <= 0:
        return result
    pool = [
        c
        for c in candidates(positions, quotes, config, flagged or set(), margin_freed)
        if c.el_gain > 0
    ]
    # Flagged first, then the cheapest margin (highest EL gain per $ of cost).
    pool.sort(key=lambda c: (not c.flagged, -c.score))
    for cand in pool:
        if result.planned_gain >= need:
            break
        result.closes.append(cand)
        result.planned_gain += cand.el_gain
    return result


def close_intent(
    cand: CloseCandidate, today: date, now: datetime, reason: str
) -> TradeIntent:
    c = cand.position.contract
    return TradeIntent(
        intent_id=make_intent_id(STRATEGY, IntentKind.CLOSE, c.key, today),
        strategy=STRATEGY,
        kind=IntentKind.CLOSE,
        legs=(OrderLeg(c, Action.BUY, int(abs(cand.position.quantity))),),
        trigger="MARGIN_RESTORATION",
        created_at=now,
        risk_reducing=True,
        details={
            "reason": reason,
            "margin_freed": cand.margin_freed,
            "cost": cand.cost,
            "flagged": cand.flagged,
        },
    )


@dataclass
class RestorationResult:
    plan: RestorationPlan
    executed: List[object] = field(default_factory=list)  # ExecutionResult
    gained: float = 0.0

    @property
    def restored(self) -> bool:
        return self.gained >= self.plan.need


ExecuteFn = Callable[[TradeIntent, bool], Awaitable[object]]


async def restore(
    restoration_plan: RestorationPlan,
    execute: ExecuteFn,
    emergency: bool,
    today: date,
    now: Optional[datetime] = None,
) -> RestorationResult:
    """Execute the plan in order; stop once the filled closes cover the need.

    ``execute(intent, emergency)`` is normally ``OrderManager.execute`` bound
    to quotes and the risk gate. Emergency closes may go up to the natural
    price (spec §6.5).
    """
    now = now or utcnow()
    result = RestorationResult(restoration_plan)
    reason = f"EL short by ${restoration_plan.need:,.0f}"
    for cand in restoration_plan.closes:
        if result.restored:
            break
        res = await execute(close_intent(cand, today, now, reason), emergency)
        result.executed.append(res)
        status = getattr(getattr(res, "status", None), "value", "")
        qty = abs(cand.position.quantity)
        filled = getattr(res, "filled", 0) or 0
        if status in ("filled", "partial", "already_done") and qty:
            share = 1.0 if status != "partial" else min(1.0, filled / qty)
            result.gained += cand.el_gain * share
        log.warning(
            "margin restoration: %s %s -> %s (planned gain $%.0f)",
            cand.position.contract.key,
            cand.position.quantity,
            status,
            cand.el_gain,
        )
    return result
