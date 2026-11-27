"""Wings: free margin without buying options back (the owner's rule).

Under Reg-T a naked short put needs ``max(20% x S - OTM, 10% x K) + premium``
per share; a put credit spread (the same short with a long put below it, same
expiry) needs only the **width**. Buying a cheap put a few strikes under a
naked short ("a wing") therefore frees most of its margin for a fraction of
what buying the short back would cost, and it caps the crash loss too.

For each naked short put the best wing is the long strike with the most
**net Excess Liquidity gained** (margin freed - cost), among strikes 1-15% of
the underlying below the short. Wings are bought greediest first (the most
EL per dollar) until the gap to the restore target is covered or the day's
budget (``margin_floor.wing_budget_pct_nlv`` of NLV) is spent.

The engine buys wings on its own when margin is below the floor and
automatic buy-backs are off (``margin_floor.auto: false``); in an emergency
it still closes positions (faster and certain). Every wing goes through the
risk gate and the order ladder like any order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    TradeIntent,
)
from src.trading.risk.limits import exposures_from, pair_put_spreads
from src.trading.risk.margin_type import reg_t_short_option_margin

STRATEGY = "margin_restoration"
MIN_BELOW, MAX_BELOW = 0.01, 0.15  # wing strike: 1-15% of the underlying below
MIN_EFFICIENCY = 2.0  # at least $2 of margin freed per $1 spent


@dataclass(frozen=True)
class Wing:
    short: Position
    long: OptionContract
    quote: Quote
    contracts: int
    cost: float  # $ for all contracts, at the ask
    freed: float  # $ of margin requirement freed

    @property
    def net(self) -> float:
        return self.freed - self.cost

    @property
    def efficiency(self) -> float:
        return self.freed / self.cost if self.cost > 0 else float("inf")

    def to_dict(self) -> dict:
        return {
            "short": self.short.contract.key,
            "long": self.long.key,
            "contracts": self.contracts,
            "ask": self.quote.ask,
            "cost": self.cost,
            "freed": self.freed,
            "net": self.net,
        }


def naked_puts(positions: Sequence[Position], quotes: Dict[str, Quote]) -> List[tuple]:
    """(position, uncovered contracts) for each short put without a wing."""
    pos_by_key = {p.contract.key: p for p in positions}
    out = []
    for e, _pairs, left in pair_put_spreads(exposures_from(positions, quotes)):
        n = int(left + 1e-9)
        if n > 0:
            out.append((pos_by_key[e.contract.key], n))
    return out


def wing_strikes(short: OptionContract, underlying: float, strikes: Sequence[float]):
    lo = underlying * (1 - MAX_BELOW)
    hi = min(short.strike, underlying) - underlying * MIN_BELOW
    hi = min(hi, short.strike - 1e-9)
    return [k for k in strikes if lo <= k <= hi]


def best_wing(
    short: Position, contracts: int, quotes: Dict[str, Quote], candidates
) -> Optional[Wing]:
    """The wing (from quoted ``candidates`` contracts) that gains the most EL."""
    c = short.contract
    sq = quotes.get(c.key)
    if sq is None or sq.underlying_price is None:
        return None
    und = sq.underlying_price
    premium = sq.mid if sq.mid is not None else (short.market_price or 0.0)
    naked = reg_t_short_option_margin(c, und, premium) * contracts
    best = None
    for lc in candidates:
        q = quotes.get(lc.key)
        if q is None or not q.ask or q.ask <= 0:
            continue
        width = (c.strike - lc.strike) * c.multiplier * contracts
        freed = naked - width
        cost = q.ask * lc.multiplier * contracts
        if freed <= cost or freed / cost < MIN_EFFICIENCY:
            continue
        w = Wing(short, lc, q, contracts, cost, freed)
        if best is None or w.net > best.net:
            best = w
    return best


def choose(wings: Sequence[Wing], need: float, budget: float) -> List[Wing]:
    """Most EL per dollar first, until ``need`` is covered or ``budget`` is spent."""
    out, gained, spent = [], 0.0, 0.0
    for w in sorted(wings, key=lambda w: -w.efficiency):
        if gained >= need:
            break
        if spent + w.cost > budget:
            continue
        out.append(w)
        gained += w.net
        spent += w.cost
    return out


def intents(wings: Sequence[Wing], now: datetime) -> List[TradeIntent]:
    out = []
    for w in wings:
        iid = f"wing-{w.long.key}-{now:%Y%m%d}".replace(" ", "_")
        out.append(
            TradeIntent(
                iid,
                STRATEGY,
                IntentKind.HEDGE,
                (OrderLeg(w.long, Action.BUY, w.contracts),),
                "MARGIN_WING",
                now,
                risk_reducing=True,
                details={"wing_for": w.short.contract.key, **w.to_dict()},
            )
        )
    return out


def put_contract(short: OptionContract, strike: float) -> OptionContract:
    """A put like ``short`` (same root, expiry, class) at another strike."""
    return replace(short, strike=float(strike))
