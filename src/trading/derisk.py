"""De-risk plan: buy back the short options that make a crash dangerous.

For an account carrying more short options than it can survive, in this
order:

1. **Naked calls**: short calls beyond the shares held in this account
   (the lowest strikes count as covered first). Unlimited risk if the stock
   jumps. Calls covered by shares at another broker are still naked for IB's
   margin; they're listed but marked as covered elsewhere.
2. **Short puts, most exposure removed per $ first**, until the account's
   short-put exposure (strike x 100 x contracts, a put spread at its width) is
   within ``risk.short_put_notional_max`` (50% of NLV). Far out-of-the-money
   puts are cheap to buy back but carry most of the crash risk.
3. **Penny calls** (worth <= $0.05): buying them back costs almost nothing
   and frees the shares for monthly covered calls.

Buying back at the market price doesn't lower the account value (the
account already counts the options at that price) except for the spread
and fees; it needs cash, and removes future risk.

Each item waits for the owner's approval (asked once, open for the trading
day), then goes through the normal order path. They're risk-reducing, so the
risk gate's allocation limits don't block them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.risk.limits import exposures_from, net_put_notional

STRATEGY = "derisk"
PENNY = 0.05


@dataclass
class Item:
    kind: str  # naked_call | put | penny_call
    position: Position
    contracts: int
    price: float  # per share, what buying back is expected to cost
    mark: float  # per share, the account's current price
    removes: float  # $ of stock exposure removed (puts: strike x 100 x n)
    reason: str
    optional: bool = False

    @property
    def cost(self) -> float:
        return self.price * self.position.contract.multiplier * self.contracts

    @property
    def friction(self) -> float:
        return max(
            0.0,
            (self.price - self.mark)
            * self.position.contract.multiplier
            * self.contracts,
        )


@dataclass
class DeriskPlan:
    plan_id: str
    request_id: str
    created_at: datetime
    nlv: float
    put_exposure_before: float
    put_exposure_after: float
    put_cap: float
    items: List[Item] = field(default_factory=list)
    intents: List[TradeIntent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def cash_needed(self) -> float:
        return sum(i.cost for i in self.items if not i.optional)

    @property
    def friction(self) -> float:
        return sum(i.friction for i in self.items if not i.optional)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat(),
            "nlv": self.nlv,
            "put_exposure_before": self.put_exposure_before,
            "put_exposure_after": self.put_exposure_after,
            "put_cap": self.put_cap,
            "cash_needed": self.cash_needed,
            "friction": self.friction,
            "items": [
                {
                    "kind": i.kind,
                    "key": i.position.contract.key,
                    "contracts": i.contracts,
                    "price": i.price,
                    "cost": i.cost,
                    "removes": i.removes,
                    "reason": i.reason,
                    "optional": i.optional,
                }
                for i in self.items
            ],
            "intents": self.intents,
            "notes": self.notes,
        }


def _prices(pos: Position, quotes: Dict[str, Quote]) -> tuple:
    """(expected buy-back price, current mark) per share."""
    q = quotes.get(pos.contract.key)
    mark = (
        (q.mid if q is not None and q.mid is not None else None)
        or pos.market_price
        or 0.0
    )
    if q is not None and q.ask and q.bid is not None and q.ask > q.bid:
        price = q.ask  # a live two-sided market: buying back pays the ask
    else:
        price = max(mark * 1.05, mark + 0.01)  # no live ask: assume a 5% spread
    return price, mark


def put_exposure(positions: Sequence[Position]) -> float:
    return sum(n for _, n in net_put_notional(exposures_from(positions, {})))


def _shares(positions: Sequence[Position]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for p in positions:
        if isinstance(p.contract, StockContract) and p.quantity > 0:
            out[p.contract.symbol] = out.get(p.contract.symbol, 0.0) + p.quantity
    return out


def plan(
    positions: Sequence[Position],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    request_id: str = "",
    now: Optional[datetime] = None,
    external_shares: Optional[Dict[str, float]] = None,
    include_penny_calls: bool = True,
) -> DeriskPlan:
    now = now or datetime.now()
    external_shares = {k.upper(): v for k, v in (external_shares or {}).items()}
    nlv = account.net_liquidation
    cap = config.risk.short_put_notional_max * nlv
    before = put_exposure(positions)
    plan_id = hashlib.sha256(f"{request_id}|{now.isoformat()}".encode()).hexdigest()[
        :12
    ]
    out = DeriskPlan(plan_id, request_id, now, nlv, before, before, cap)
    shares = _shares(positions)

    # 1. Naked calls: cover the lowest strikes first with the shares held here.
    calls: Dict[str, List[Position]] = {}
    for p in positions:
        c = p.contract
        if isinstance(c, OptionContract) and c.right is Right.CALL and p.quantity < 0:
            calls.setdefault(c.underlying, []).append(p)
    covered_calls: List[Position] = []
    for und, plist in calls.items():
        free = int(shares.get(und, 0) // 100)
        for p in sorted(plist, key=lambda x: x.contract.strike):
            n = int(abs(p.quantity))
            covered = min(n, free)
            free -= covered
            if covered:
                covered_calls.append(p)
            naked = n - covered
            if naked <= 0:
                continue
            price, mark = _prices(p, quotes)
            elsewhere = external_shares.get(und, 0) >= naked * 100
            out.items.append(
                Item(
                    "naked_call",
                    p,
                    naked,
                    price,
                    mark,
                    0.0,
                    (
                        "covered by your shares at the other broker, but IB margins it as naked here"
                        if elsewhere
                        else "no shares behind it: unlimited risk if the stock jumps"
                    ),
                    optional=elsewhere,
                )
            )

    # 2. Short puts, most exposure removed per $ first, down to the cap.
    remaining = list(positions)
    after = before
    puts = [
        p
        for p in positions
        if isinstance(p.contract, OptionContract)
        and p.contract.right is Right.PUT
        and p.quantity < 0
    ]

    def ratio(p: Position) -> float:
        price, _ = _prices(p, quotes)
        cost = price * p.contract.multiplier * abs(p.quantity)
        return (
            p.contract.strike * p.contract.multiplier * abs(p.quantity) / max(cost, 1.0)
        )

    for p in sorted(puts, key=ratio, reverse=True):
        if after <= cap:
            break
        price, mark = _prices(p, quotes)
        trial = [x for x in remaining if x is not p]
        new_after = put_exposure(trial)
        if new_after >= after - 1e-6:
            continue  # part of a spread already netted: closing it alone removes nothing
        out.items.append(
            Item(
                "put",
                p,
                int(abs(p.quantity)),
                price,
                mark,
                after - new_after,
                f"removes ${after - new_after:,.0f} of exposure for ${price * p.contract.multiplier * abs(p.quantity):,.0f}",
            )
        )
        remaining, after = trial, new_after
    out.put_exposure_after = after
    if after > cap:
        out.notes.append(
            f"Even after these, short-put exposure is ${after:,.0f} (limit ${cap:,.0f})."
        )

    # 3. Penny calls on shares: free them for monthly covered calls.
    if include_penny_calls:
        for p in covered_calls:
            price, mark = _prices(p, quotes)
            if mark <= PENNY:
                out.items.append(
                    Item(
                        "penny_call",
                        p,
                        int(abs(p.quantity)),
                        price,
                        mark,
                        0.0,
                        f"worth ${mark:.2f}: frees {int(abs(p.quantity)) * 100} shares for monthly covered calls",
                        optional=True,
                    )
                )
    return out


def intents(p: DeriskPlan, config: TradingConfig, today: date) -> List[TradeIntent]:
    out = []
    for i, item in enumerate(p.items):
        c = item.position.contract
        iid = hashlib.sha256(f"derisk|{p.plan_id}|{c.key}".encode()).hexdigest()[:16]
        out.append(
            TradeIntent(
                iid,
                STRATEGY,
                IntentKind.CLOSE,
                (OrderLeg(c, Action.BUY, item.contracts),),
                f"DERISK_{item.kind.upper()}",
                p.created_at,
                True,
                None,
                {
                    "require_approval": True,
                    "ask_once": True,
                    "approval_ttl_s": config.rebuild.approval_hours * 3600,
                    "derisk_plan": p.plan_id,
                    "worst_price": round(max(item.price * 1.10, item.price + 0.05), 2),
                    "reason": item.reason,
                    "removes": item.removes,
                    "est_cost": item.cost,
                    "optional": item.optional,
                    "order": i + 1,
                },
            )
        )
    return out
