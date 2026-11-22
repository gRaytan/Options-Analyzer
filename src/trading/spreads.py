"""Managing put credit spreads (plan T34, spec §5.4).

A spread is opened as one combo (T33-T34 entries) but IB reports it as two
positions: a short put and a long put on the same root and expiry at a
lower strike. This module pairs them back up, so the same exit, guard and
roll rules (§5.2) act on the **whole spread**:

* **Exits** (position manager: stop loss, gamma exit, replacement) decide on
  the short leg, as for any short put, and close the spread as **one combo**
  (buy the short, sell the long): no orphan long put left behind.
* **Guard rolls** close the spread instead of rolling it. Rolling only the
  short leg would leave a naked put; index spreads can't be assigned, so
  closing on purpose is the safe way to take the loss.
* **Margin restoration** (T26) still buys back short legs alone when it must
  free margin fast; that only reduces risk.

Only positions the bot manages (from its own fills) are paired: the
owner's own long puts (e.g. a hedge) are never sold to close a spread.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Dict, Iterable, Optional, Set

from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Right,
    TradeIntent,
    make_intent_id,
    utcnow,
)

STRATEGY = "put_spread"


def _group(c: OptionContract) -> tuple:
    return ((c.trading_class or c.underlying).upper(), c.expiry, c.multiplier)


def pair_spreads(
    positions: Iterable[Position], managed: Optional[Set[str]] = None
) -> Dict[str, Position]:
    """short-put key -> the long put that caps it (managed positions only).

    A long pairs with a short on the same root/expiry at a lower strike, with
    at least as many contracts; the highest such long strike wins, and each
    long is used once. ``managed=None`` pairs everything (tests, reports).
    """
    shorts, longs = [], []
    for p in positions:
        c = p.contract
        if not isinstance(c, OptionContract) or c.right is not Right.PUT:
            continue
        if managed is not None and c.key not in managed:
            continue
        (shorts if p.quantity < 0 else longs).append(p)
    used: Set[str] = set()
    out: Dict[str, Position] = {}
    for s in sorted(shorts, key=lambda p: -p.contract.strike):
        best = None
        for lg in longs:
            lc = lg.contract
            if (
                lc.key in used
                or _group(lc) != _group(s.contract)
                or lc.strike >= s.contract.strike
                or lg.quantity < abs(s.quantity)
            ):
                continue
            if best is None or lc.strike > best.contract.strike:
                best = lg
        if best is not None:
            used.add(best.contract.key)
            out[s.contract.key] = best
    return out


def as_spread_close(intent: TradeIntent, pairs: Dict[str, Position]) -> TradeIntent:
    """Widen a single-leg buy-back of a spread's short put into a combo close."""
    if len(intent.legs) != 1:
        return intent
    [leg] = intent.legs
    long_pos = pairs.get(leg.contract.key)
    if leg.action is not Action.BUY or long_pos is None:
        return intent
    return replace(
        intent,
        legs=(leg, OrderLeg(long_pos.contract, Action.SELL, leg.quantity)),
        details={**intent.details, "spread_long": long_pos.contract.key},
    )


def close_intent(
    short: Position,
    long_pos: Position,
    reason: str,
    today: date,
    now: Optional[datetime] = None,
    chain_id: Optional[str] = None,
) -> TradeIntent:
    """Close the whole spread as one combo (risk-reducing)."""
    qty = int(abs(short.quantity))
    return TradeIntent(
        make_intent_id(STRATEGY, IntentKind.CLOSE, short.contract.key, today),
        STRATEGY,
        IntentKind.CLOSE,
        (
            OrderLeg(short.contract, Action.BUY, qty),
            OrderLeg(long_pos.contract, Action.SELL, qty),
        ),
        "SPREAD_CLOSE",
        now or utcnow(),
        True,
        chain_id,
        {"reason": reason, "spread_long": long_pos.contract.key},
    )
