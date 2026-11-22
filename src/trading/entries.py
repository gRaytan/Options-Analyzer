"""Entries: from "ready" scanner candidates to opening intents (plan T33-T34, spec §5.2-5.4).

Each scan (T30), ready candidates (timing >= 1.1, T31) become OPEN intents:

* **Covered calls:** quantity = the uncovered lots under the coverage cap
  (already computed by the scanner).
* **Index and single-stock puts:** sized so their crash-scenario loss fits
  the **free** risk budget (budget - what the book already uses), capped by
  the per-order contract limit and by the notional room left under the
  short-put, index-put and per-expiry caps. No room = no trade.
* **Put credit spreads instead of naked index puts** (T34, spec §5.4) when
  the VIX regime is spreads-only (spike fading) or Cushion is below
  ``spread_cushion_below`` (50%): defined risk when risk matters most. A
  spread is one combo order (sell the short put, buy the lower-strike put),
  sized so its max loss is <= ``spread_max_loss_nlv`` (1%) of NLV, within
  the free budget and the caps (where it counts at its width, not its strike).
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
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.fees import estimate_order_fees
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from, net_put_notional
from src.trading.scanner import Candidate, ScanResult

PUT_STRATEGIES = {
    StrategyName.INDEX_PUT,
    StrategyName.STOCK_PUT,
    StrategyName.PUT_SPREAD,
}


def contracts_cap(contract: OptionContract, config: TradingConfig) -> int:
    root = (contract.trading_class or contract.underlying).upper()
    r = config.risk
    if root in ("SPX", "SPXW"):
        return r.max_contracts_spx
    if root == "XSP":
        return r.max_contracts_xsp
    return r.max_contracts_per_order


def cap_notional(cand: Candidate) -> float:
    """$ per contract this candidate adds to the put caps (a spread: its width)."""
    c = cand.contract
    return (cand.width if cand.is_spread else c.strike) * c.multiplier


def size_put(
    cand: Candidate,
    free_budget: float,
    config: TradingConfig,
    room: Optional[Dict[str, float]] = None,
    nlv: float = 0.0,
) -> int:
    """Contracts that fit the free crash budget, the contract cap, the notional
    room left under the put caps (``room``: $ left per cap) and, for spreads,
    the max-loss cap (``spread_max_loss_nlv`` x ``nlv``)."""
    if cand.stress_loss <= 0 or free_budget <= 0:
        return 0
    qty = min(
        int(free_budget // cand.stress_loss), contracts_cap(cand.contract, config)
    )
    if room:
        notional = cap_notional(cand)
        for left in room.values():
            qty = min(qty, int(max(left, 0.0) // notional))
    if cand.is_spread and nlv > 0:
        max_loss = cand.max_loss or 0.0
        if max_loss <= 0:
            return 0
        qty = min(qty, int(config.entry.spread_max_loss_nlv * nlv // max_loss))
    return max(0, qty)


def spread_mode(
    budget: Optional[dict], cushion: Optional[float], config: TradingConfig
) -> bool:
    """Put spreads instead of naked index puts (spec §5.4)?"""
    if budget and budget.get("spreads_only"):
        return True
    return cushion is not None and cushion < config.entry.spread_cushion_below


def notional_room(
    positions: Sequence[Position],
    nlv: float,
    config: TradingConfig,
    contract: OptionContract,
) -> Dict[str, float]:
    """$ of short-put notional still allowed: total, index, this expiry (spec §6.1).

    Measured as the risk gate measures it (spreads net to their width).
    """
    r = config.risk
    total = index = expiry = 0.0
    for e, n in net_put_notional(exposures_from(positions, {})):
        total += n
        if e.is_index:
            index += n
        if e.contract.expiry == contract.expiry:
            expiry += n
    put_cap = r.short_put_notional_max * nlv
    room = {"total": put_cap - total, "expiry": r.expiry_max_share * put_cap - expiry}
    probe = exposures_from([Position(contract, -1, 0.0)], {})[0]
    if probe.is_index:
        room["index"] = r.index_put_notional_max * nlv - index
    return room


def legs_for(cand: Candidate, qty: int) -> tuple:
    legs = (OrderLeg(cand.contract, Action.SELL, qty),)
    if cand.is_spread:
        legs += (OrderLeg(cand.long_contract, Action.BUY, qty),)
    return legs


def worst_price(cand: Candidate, qty: int, config: TradingConfig) -> float:
    """The least net credit per share the order may accept (as a negative price)."""
    c = cand.contract
    mids = {c.key: cand.quote.mid}
    net = cand.quote.mid
    if cand.is_spread:
        mids[cand.long_contract.key] = cand.long_quote.mid
        net -= cand.long_quote.mid
    probe = OrderRequest("probe", legs_for(cand, qty), -net, "probe")
    fees = estimate_order_fees(probe, mids, config.fees).total
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


def _allowed(cand: Candidate, budget: Optional[dict], spreads: bool) -> bool:
    """May this put candidate be entered in the current mode?"""
    if budget is None:
        return False  # unknown risk budget: no new put risk
    if cand.is_spread or cand.strategy is StrategyName.INDEX_PUT:
        return cand.is_spread == spreads  # spreads replace naked index puts
    return not budget.get("spreads_only")  # stock puts: not in a VIX spike


def entry_intents(
    result: ScanResult,
    budget: Optional[dict],
    config: TradingConfig,
    journal: Journal,
    today: date,
    now: Optional[datetime] = None,
    positions: Sequence[Position] = (),
    nlv: float = 0.0,
    excess_liquidity: Optional[float] = None,
) -> List[TradeIntent]:
    """Opening intents for this scan's ready candidates (may be empty).

    With ``nlv`` (and the current ``positions``), put sizes also respect the
    notional caps (total, index, per expiry) so the gate isn't asked for the
    impossible; with ``excess_liquidity``, a Cushion under 50% switches index
    puts to spreads.
    """
    held = list(positions)
    cushion = (
        excess_liquidity / nlv if excess_liquidity is not None and nlv > 0 else None
    )
    spreads = spread_mode(budget, cushion, config)
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
            if not _allowed(cand, budget, spreads):
                continue
            room = notional_room(held, nlv, config, cand.contract) if nlv > 0 else None
            qty = size_put(cand, free, config, room, nlv)
            if qty <= 0:
                continue
            free -= qty * cand.stress_loss
            held.append(Position(cand.contract, -qty, cand.quote.mid))
            if cand.is_spread:
                held.append(Position(cand.long_contract, qty, cand.long_quote.mid))
        else:
            qty = min(cand.contracts, contracts_cap(cand.contract, config))
            if qty <= 0:
                continue
        seen[who] = True
        details = {
            "worst_price": worst_price(cand, qty, config),
            "score": cand.score,
            "ev": cand.ev,
            "timing": cand.timing,
            "timing_reasons": list(cand.timing_reasons),
            "expected_credit": cand.credit * qty,
        }
        if cand.is_spread:
            details.update(width=cand.width, max_loss=(cand.max_loss or 0.0) * qty)
        intents.append(
            TradeIntent(
                intent_id=make_intent_id(
                    cand.strategy.value, IntentKind.OPEN, cand.contract.key, today
                ),
                strategy=cand.strategy.value,
                kind=IntentKind.OPEN,
                legs=legs_for(cand, qty),
                trigger="SCAN_READY",
                created_at=now,
                risk_reducing=False,
                details=details,
            )
        )
    return intents
