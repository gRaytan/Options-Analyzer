"""Per-order sanity checks (plan T17, spec §6.3).

These run on **every** order (opening, closing, rolls, hedges, margin
restoration), and they're pure functions of the order and a market/
portfolio context, so they can be tested exhaustively.

| Check | Rule |
|---|---|
| ``halt`` | Opening needs no halt; closing needs no full halt (§6.4) |
| ``matches_intent`` | The order's legs are exactly the intent's legs |
| ``quotes_live`` | Every leg has a quote, it isn't delayed, and it's < 5 s old |
| ``price_in_market`` | The net limit is within the legs' combined bid/ask |
| ``no_flip`` | No leg closes more than the position, which would flip it |
| ``no_duplicate`` | No working order already touches the same contract |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from src.trading.domain import (
    Action,
    HaltState,
    HaltScope,
    IntentKind,
    OrderRequest,
    Quote,
    TradeIntent,
)
from src.trading.risk.checks import CheckResult, GateDecision

MAX_QUOTE_AGE_S = 5.0
PRICE_EPSILON = 1e-6


@dataclass
class OrderContext:
    now: datetime
    quotes: Dict[str, Quote]  # contract key -> latest quote
    positions: Dict[str, float]  # contract key -> signed quantity
    working_keys: Set[str] = field(default_factory=set)  # contracts in working orders
    halt: HaltState = field(default_factory=lambda: HaltState(False))
    max_quote_age_s: float = MAX_QUOTE_AGE_S


def _opens_risk(intent: TradeIntent) -> bool:
    return (
        intent.kind in (IntentKind.OPEN, IntentKind.HEDGE) and not intent.risk_reducing
    )


def check_halt(intent: TradeIntent, ctx: OrderContext) -> CheckResult:
    halt = ctx.halt
    if not halt.active:
        return CheckResult("halt", True)
    if halt.scope is HaltScope.ALL:
        return CheckResult("halt", False, f"full halt: {halt.reason}")
    if _opens_risk(intent):
        return CheckResult("halt", False, f"opening halted: {halt.reason}")
    return CheckResult("halt", True, "opening halted, but this order reduces risk")


def check_matches_intent(order: OrderRequest, intent: TradeIntent) -> CheckResult:
    if order.intent_id != intent.intent_id:
        return CheckResult("matches_intent", False, "order belongs to another intent")
    if order.order_ref != intent.intent_id:
        return CheckResult(
            "matches_intent", False, "orderRef must equal the intent ID (§7.2)"
        )
    if tuple(order.legs) != tuple(intent.legs):
        return CheckResult("matches_intent", False, "legs differ from the intent")
    return CheckResult("matches_intent", True)


def check_quotes_live(order: OrderRequest, ctx: OrderContext) -> CheckResult:
    problems: List[str] = []
    for leg in order.legs:
        q = ctx.quotes.get(leg.contract.key)
        if q is None:
            problems.append(f"{leg.contract.key}: no quote")
        elif q.delayed:
            problems.append(f"{leg.contract.key}: delayed data")
        elif q.age_seconds(ctx.now) > ctx.max_quote_age_s:
            problems.append(
                f"{leg.contract.key}: quote {q.age_seconds(ctx.now):.0f}s old"
            )
        elif q.mid is None:
            problems.append(f"{leg.contract.key}: no two-sided market")
    if problems:
        return CheckResult("quotes_live", False, "; ".join(problems))
    return CheckResult("quotes_live", True)


def net_market(order: OrderRequest, quotes: Dict[str, Quote]) -> Optional[tuple]:
    """(best, natural) net debit per unit. A credit is negative.

    ``natural`` is the worst price (buy at asks, sell at bids); ``best`` is
    the other side (buy at bids, sell at asks).
    """
    best = natural = 0.0
    for leg in order.legs:
        q = quotes.get(leg.contract.key)
        if q is None or q.bid is None or q.ask is None:
            return None
        if leg.action is Action.BUY:
            best += q.bid
            natural += q.ask
        else:
            best -= q.ask
            natural -= q.bid
    return best, natural


def check_price_in_market(order: OrderRequest, ctx: OrderContext) -> CheckResult:
    market = net_market(order, ctx.quotes)
    if market is None:
        return CheckResult("price_in_market", False, "no market to price against")
    best, natural = market
    if order.limit_price > natural + PRICE_EPSILON:
        return CheckResult(
            "price_in_market",
            False,
            f"limit {order.limit_price:.2f} is worse than the natural price {natural:.2f}",
        )
    if order.limit_price < best - PRICE_EPSILON:
        return CheckResult(
            "price_in_market",
            False,
            f"limit {order.limit_price:.2f} is outside the market (best {best:.2f})",
        )
    return CheckResult(
        "price_in_market",
        True,
        f"limit {order.limit_price:.2f} in [{best:.2f}, {natural:.2f}]",
    )


def check_no_flip(order: OrderRequest, ctx: OrderContext) -> CheckResult:
    for leg in order.legs:
        held = ctx.positions.get(leg.contract.key, 0.0)
        change = leg.action.sign * leg.quantity
        if (
            held
            and (held > 0) != (change > 0)
            and abs(change) > abs(held) + PRICE_EPSILON
        ):
            return CheckResult(
                "no_flip",
                False,
                f"{leg.contract.key}: order {change:+g} would flip position {held:+g}",
            )
    return CheckResult("no_flip", True)


def check_reduces_position(intent: TradeIntent, ctx: OrderContext) -> CheckResult:
    """Closes and risk-reducing intents must actually reduce held positions.

    They skip the opening halt and the §6.1 limits, so this is what stops a
    mislabeled "close" from opening new risk (validation finding 1).
    Rolls are checked separately: their opening leg passes the limits.
    """
    if intent.kind is not IntentKind.CLOSE and not intent.risk_reducing:
        return CheckResult("reduces_position", True, "not a close")
    if intent.kind is IntentKind.ROLL:
        return CheckResult(
            "reduces_position", True, "roll: opening leg checked by limits"
        )
    for leg in intent.legs:
        held = ctx.positions.get(leg.contract.key, 0.0)
        change = leg.action.sign * leg.quantity
        if not held or (held > 0) == (change > 0):
            return CheckResult(
                "reduces_position",
                False,
                f"{leg.contract.key}: a close must reduce a held position "
                f"(held {held:+g}, order {change:+g})",
            )
        if abs(change) > abs(held) + PRICE_EPSILON:
            return CheckResult(
                "reduces_position",
                False,
                f"{leg.contract.key}: closes {abs(change):g}, only {abs(held):g} held",
            )
    return CheckResult("reduces_position", True)


def check_no_duplicate(order: OrderRequest, ctx: OrderContext) -> CheckResult:
    busy = sorted({leg.contract.key for leg in order.legs} & ctx.working_keys)
    if busy:
        return CheckResult(
            "no_duplicate", False, f"working order already on {', '.join(busy)}"
        )
    return CheckResult("no_duplicate", True)


def sanity_checks(
    order: OrderRequest, intent: TradeIntent, ctx: OrderContext
) -> GateDecision:
    """Run every §6.3 check and return all results (not just the first failure)."""
    return GateDecision(
        (
            check_halt(intent, ctx),
            check_matches_intent(order, intent),
            check_quotes_live(order, ctx),
            check_price_in_market(order, ctx),
            check_no_flip(order, ctx),
            check_reduces_position(intent, ctx),
            check_no_duplicate(order, ctx),
        )
    )


def working_contract_keys(orders: Iterable) -> Set[str]:
    """Contract keys touched by working orders (``OrderState`` objects)."""
    keys: Set[str] = set()
    for state in orders:
        if not state.status.is_done:
            keys.update(leg.contract.key for leg in state.order.legs)
    return keys
