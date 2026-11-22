"""Entries: from "ready" scanner candidates to opening intents (plan T33, spec §5.2-5.3).

Each scan (T30), ready candidates (timing >= 1.1, T31) become OPEN intents:

* **Covered calls:** quantity = the uncovered lots under the coverage cap
  (already computed by the scanner).
* **Index and single-stock puts:** sized so their crash-scenario loss fits
  the **free** risk budget (budget - what the book already uses), capped by
  the per-order contract limit. No room = no trade.
* At most **one new position per underlying per day** (fewer, larger trades,
  R2), and the best candidate per underlying first.
* Every entry carries a hard ``worst_price``: the ladder never sells for less
  than the minimum net credit (``min_net_credit_per_contract``) plus fees.

The executor (T28) then routes each intent: paper sends, approve/auto ask
unless ``auto_open``, shadow logs; the risk gate re-checks everything
(margin after the trade, caps, ladder, stress) at order time.

Index puts use the XSP root (PM-settled; AM-settled SPX monthlies are never
scanned). Switching to SPXW at large size is a later refinement.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Set

from src.trading.config import StrategyName, TradingConfig
from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Right,
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.fees import estimate_order_fees
from src.trading.journal import Journal
from src.trading.risk.limits import INDEX_ROOTS as LIMIT_INDEX_ROOTS
from src.trading.scanner import Candidate, ScanResult

PUT_STRATEGIES = {StrategyName.INDEX_PUT, StrategyName.STOCK_PUT}


def contracts_cap(contract: OptionContract, config: TradingConfig) -> int:
    root = (contract.trading_class or contract.underlying).upper()
    r = config.risk
    if root in ("SPX", "SPXW"):
        return r.max_contracts_spx
    if root == "XSP":
        return r.max_contracts_xsp
    return r.max_contracts_per_order


def size_put(
    cand: Candidate,
    free_budget: float,
    config: TradingConfig,
    room: Optional[Dict[str, float]] = None,
) -> int:
    """Contracts that fit the free crash budget, the contract cap and the
    notional room left under the put caps (``room``: $ left per cap)."""
    if cand.stress_loss <= 0 or free_budget <= 0:
        return 0
    qty = min(
        int(free_budget // cand.stress_loss), contracts_cap(cand.contract, config)
    )
    if room:
        notional = cand.contract.strike * cand.contract.multiplier
        for left in room.values():
            qty = min(qty, int(max(left, 0.0) // notional))
    return max(0, qty)


def _is_index(c: OptionContract) -> bool:
    return (c.trading_class or c.underlying).upper() in LIMIT_INDEX_ROOTS or (
        c.underlying.upper() in LIMIT_INDEX_ROOTS
    )


def notional_room(
    positions: Sequence[Position],
    nlv: float,
    config: TradingConfig,
    contract: OptionContract,
) -> Dict[str, float]:
    """$ of short-put notional still allowed: total, index, this expiry (spec §6.1)."""
    r = config.risk
    total = index = expiry = 0.0
    for p in positions:
        c = p.contract
        if isinstance(c, OptionContract) and c.right is Right.PUT and p.quantity < 0:
            n = c.strike * c.multiplier * abs(p.quantity)
            total += n
            if _is_index(c):
                index += n
            if c.expiry == contract.expiry:
                expiry += n
    put_cap = r.short_put_notional_max * nlv
    room = {"total": put_cap - total, "expiry": r.expiry_max_share * put_cap - expiry}
    if _is_index(contract):
        room["index"] = r.index_put_notional_max * nlv - index
    return room


def worst_price(cand: Candidate, qty: int, config: TradingConfig) -> float:
    """The least credit per share the order may accept (as a negative price)."""
    c = cand.contract
    probe = OrderRequest(
        "probe", (OrderLeg(c, Action.SELL, qty),), -cand.quote.mid, "probe"
    )
    fees = estimate_order_fees(probe, {c.key: cand.quote.mid}, config.fees).total
    floor = (config.entry.min_net_credit_per_contract * qty + 2 * fees) / (
        qty * c.multiplier
    )
    return -round(floor, 2)


def entered_today(journal: Journal, today: date) -> Set[tuple]:
    """(strategy, underlying) pairs with an opening intent already today."""
    out = set()
    # Use the intent's own creation date (journal write times are wall-clock).
    since = datetime(
        today.year, today.month, today.day, tzinfo=utcnow().tzinfo
    ) - timedelta(days=2)
    for rec in journal.read("intents", since=since):
        it = rec.data
        if it.created_at.date() != today:
            continue
        if it.kind is IntentKind.OPEN and it.trigger == "SCAN_READY":
            out.add((it.strategy, it.legs[0].contract.underlying.upper()))
    return out


def entry_intents(
    result: ScanResult,
    budget: Optional[dict],
    config: TradingConfig,
    journal: Journal,
    today: date,
    now: Optional[datetime] = None,
    positions: Sequence[Position] = (),
    nlv: float = 0.0,
) -> List[TradeIntent]:
    """Opening intents for this scan's ready candidates (may be empty).

    With ``nlv`` (and the current ``positions``), put sizes also respect the
    notional caps (total, index, per expiry) so the gate isn't asked for the
    impossible.
    """
    held = list(positions)
    now = now or utcnow()
    free = float(budget.get("free", 0.0)) if budget else 0.0
    done = entered_today(journal, today)
    seen: Dict[tuple, bool] = {}
    intents: List[TradeIntent] = []
    for cand in result.ready():  # best first
        who = (cand.strategy.value, cand.contract.underlying.upper())
        if who in done or who in seen:
            continue
        if cand.strategy in PUT_STRATEGIES:
            if budget is None or budget.get("spreads_only"):
                continue  # rising VIX or spreads-only regime: no naked put entries (T34)
            room = notional_room(held, nlv, config, cand.contract) if nlv > 0 else None
            qty = size_put(cand, free, config, room)
            if qty <= 0:
                continue
            free -= qty * cand.stress_loss
            held.append(Position(cand.contract, -qty, cand.quote.mid))
        else:
            qty = min(cand.contracts, contracts_cap(cand.contract, config))
            if qty <= 0:
                continue
        seen[who] = True
        intents.append(
            TradeIntent(
                intent_id=make_intent_id(
                    cand.strategy.value, IntentKind.OPEN, cand.contract.key, today
                ),
                strategy=cand.strategy.value,
                kind=IntentKind.OPEN,
                legs=(OrderLeg(cand.contract, Action.SELL, qty),),
                trigger="SCAN_READY",
                created_at=now,
                risk_reducing=False,
                details={
                    "worst_price": worst_price(cand, qty, config),
                    "score": cand.score,
                    "ev": cand.ev,
                    "timing": cand.timing,
                    "timing_reasons": list(cand.timing_reasons),
                    "expected_credit": cand.credit * qty,
                },
            )
        )
    return intents
