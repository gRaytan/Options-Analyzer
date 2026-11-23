"""Rebuild plan: buy back shares and sell options after a margin call.

The owner sets target holdings on the Rebuild page (e.g. SPY 50%, QQQM
50%). The engine turns them into a **plan for this step**, in this order:

1. **Shares**, one tranche (``rebuild.tranches``, default 4) of the gap to
   each target, with no borrowing beyond ``max_gross_leverage`` x NLV
   (1.0 = stock worth at most the account). The tranche is scaled down until,
   after it, all three hold:

   * Excess Liquidity >= $125k and Cushion >= 40% (the new-trade floor);
   * in the crash scenario (-25%, IV +50%, margin x1.5) Excess Liquidity
     stays >= $100k.

   While VIX is above 30 and rising the tranche is halved
   (``rising_vix_scale``).
2. **Covered calls** on shares already held and uncovered (not on shares the
   plan is about to buy: those come in the next plan, once filled).
3. **Puts or put spreads**, only if the VIX regime allows new risk; spreads
   instead of naked puts when VIX is spiking or Cushion is under 50%.

**Every item waits for the owner's approval**, in every mode (paper too),
and is offered once: an item left to expire is not asked again; build a new
plan instead. After approval the normal path takes over: limit orders from
the mid, the risk gate (margin what-if, stress), then IB. Live accounts need
the usual two keys (spec §11).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    IntentKind,
    OrderLeg,
    Position,
    Quote,
    StockContract,
    TradeIntent,
    utcnow,
)
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import STOCK_MAINT, Scenario, StressModel

STRATEGY = "rebuild"
BISECT_STEPS = 30


def parse_targets(text: str) -> Dict[str, float]:
    """``"SPY=50, QQQM=50"`` or ``"SPY=0.5,QQQM=0.5"`` -> weights (fractions)."""
    out: Dict[str, float] = {}
    for part in text.replace(";", ",").split(","):
        if not part.strip():
            continue
        sym, _, w = part.partition("=")
        sym, value = sym.strip().upper(), float(w.strip().rstrip("%"))
        if not sym.isalnum():
            raise ValueError(f"bad symbol {sym!r}")
        out[sym] = value / 100 if value > 1 else value
    total = sum(out.values())
    if not out or total <= 0 or total > 1.0 + 1e-9:
        raise ValueError("targets must add up to more than 0% and at most 100%")
    return out


@dataclass
class StockStep:
    symbol: str
    price: float
    held_value: float
    target_value: float
    shares: int
    cost: float


@dataclass
class Fit:
    scale: float  # of the tranche that fits (0..1)
    el_after: float
    cushion_after: float
    stress_el_after: float
    gross_after: float
    binding: str  # which limit stopped it ("" = the full tranche fits)


@dataclass
class RebuildPlan:
    plan_id: str
    request_id: str
    created_at: datetime
    targets: Dict[str, float]
    nlv: float
    excess_liquidity: float
    cushion: float
    stock_value: float
    regime: str
    steps: List[StockStep] = field(default_factory=list)
    fit: Optional[Fit] = None
    intents: List[TradeIntent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat(),
            "targets": self.targets,
            "nlv": self.nlv,
            "excess_liquidity": self.excess_liquidity,
            "cushion": self.cushion,
            "stock_value": self.stock_value,
            "regime": self.regime,
            "steps": [s.__dict__ for s in self.steps],
            "fit": self.fit.__dict__ if self.fit else None,
            "intents": self.intents,
            "notes": self.notes,
        }


def _stock_value(
    positions: Sequence[Position], prices: Dict[str, float]
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for p in positions:
        c = p.contract
        if isinstance(c, StockContract) and p.quantity > 0:
            px = prices.get(c.symbol) or p.market_price or p.avg_cost
            out[c.symbol] = out.get(c.symbol, 0.0) + p.quantity * px
    return out


def evaluate(
    positions: Sequence[Position],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    buys: Dict[str, float],  # symbol -> $ to buy
    prices: Dict[str, float],
    config: TradingConfig,
    today: date,
) -> Fit:
    """Account after buying ``buys`` with cash (Reg-T): EL, Cushion, crash EL."""
    cost = sum(buys.values())
    nlv = account.net_liquidation
    maint_add = STOCK_MAINT * cost
    after = replace(
        account,
        excess_liquidity=account.excess_liquidity - maint_add,
        maint_margin=account.maint_margin + maint_add,
        cash=account.cash - cost,
    )
    added = [
        Position(StockContract(s), v / prices[s], prices[s], prices[s])
        for s, v in buys.items()
        if v > 0
    ]
    model = StressModel(
        exposures_from(list(positions) + added, quotes), quotes, after, today
    )
    mm = config.risk.stress_margin_multiplier
    crash = model.run(Scenario("worst -25% IV+50%", -0.25, 0.50, mm))
    gross = sum(_stock_value(positions, prices).values()) + cost
    return Fit(
        1.0,
        after.excess_liquidity,
        after.excess_liquidity / nlv if nlv > 0 else 0.0,
        crash.excess_liquidity_after,
        gross,
        "",
    )


def _ok(fit: Fit, nlv: float, config: TradingConfig) -> str:
    f = config.margin_floor
    if fit.el_after < f.excess_liquidity_min + f.entry_buffer:
        return "Excess Liquidity after the buy"
    if fit.cushion_after < f.cushion_min_new_trade:
        return "Cushion after the buy"
    if fit.stress_el_after < f.excess_liquidity_min:
        return "Excess Liquidity in a -25% crash"
    if fit.gross_after > config.rebuild.max_gross_leverage * nlv + 1e-6:
        return "no borrowing (stock <= account value)"
    return ""


def plan_shares(
    positions: Sequence[Position],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    prices: Dict[str, float],
    targets: Dict[str, float],
    config: TradingConfig,
    regime: str,
    today: date,
) -> tuple:
    """(steps, fit, notes): this tranche of shares, scaled to fit every limit."""
    r = config.rebuild
    nlv = account.net_liquidation
    held = _stock_value(positions, prices)
    notes: List[str] = []
    tranche: Dict[str, float] = {}
    for sym, w in targets.items():
        if not prices.get(sym):
            notes.append(f"{sym}: no live price, skipped")
            continue
        target = w * nlv * r.max_gross_leverage
        gap = max(0.0, target - held.get(sym, 0.0))
        if gap <= 0:
            notes.append(f"{sym}: already at or above its target")
            continue
        tranche[sym] = min(gap, target / r.tranches)
    scale_regime = 1.0
    if regime in ("rising", "unknown"):
        scale_regime = r.rising_vix_scale
        notes.append(
            f"VIX regime '{regime}': the share tranche is cut to {scale_regime:.0%}"
        )
    tranche = {s: v * scale_regime for s, v in tranche.items()}
    if not tranche:
        return [], None, notes

    def fit_at(s: float) -> Fit:
        return evaluate(
            positions,
            quotes,
            account,
            {k: v * s for k, v in tranche.items()},
            prices,
            config,
            today,
        )

    full = fit_at(1.0)
    binding = _ok(full, nlv, config)
    if not binding:
        best, scale = full, 1.0
    else:
        none = fit_at(0.0)
        why_none = _ok(none, nlv, config)
        if why_none:
            notes.append(
                f"No room to buy: {why_none} is already below its limit. Restore the "
                "margin cushion first (closing options or adding cash)."
            )
            none.scale, none.binding = 0.0, why_none
            return [], none, notes
        lo, hi = 0.0, 1.0
        for _ in range(BISECT_STEPS):
            mid = (lo + hi) / 2
            if _ok(fit_at(mid), nlv, config):
                hi = mid
            else:
                lo = mid
        scale = lo
        best = fit_at(scale)
        notes.append(f"Tranche cut to {scale:.0%} by: {binding}")
    best.scale, best.binding = scale, binding
    steps = []
    for sym, v in tranche.items():
        shares = int(math.floor(v * scale / prices[sym]))
        if shares < 1:
            continue
        steps.append(
            StockStep(
                sym,
                prices[sym],
                held.get(sym, 0.0),
                targets[sym] * nlv * r.max_gross_leverage,
                shares,
                shares * prices[sym],
            )
        )
    return steps, best, notes


def _plan_details(plan_id: str, config: TradingConfig, **extra) -> dict:
    return {
        "require_approval": True,
        "ask_once": True,
        "approval_ttl_s": config.rebuild.approval_hours * 3600,
        "rebuild_plan": plan_id,
        **extra,
    }


def _intent_id(plan_id: str, key: str) -> str:
    return hashlib.sha256(f"rebuild|{plan_id}|{key}".encode()).hexdigest()[:16]


def stock_intents(
    plan_id: str, steps: Sequence[StockStep], config: TradingConfig, now: datetime
) -> List[TradeIntent]:
    out = []
    for s in steps:
        out.append(
            TradeIntent(
                _intent_id(plan_id, s.symbol),
                STRATEGY,
                IntentKind.OPEN,
                (OrderLeg(StockContract(s.symbol), Action.BUY, s.shares),),
                "REBUILD",
                now,
                False,
                None,
                _plan_details(
                    plan_id,
                    config,
                    rebuild=True,
                    worst_price=round(
                        s.price * (1 + config.rebuild.worst_price_pct), 2
                    ),
                    est_cost=s.cost,
                    target_value=s.target_value,
                    held_value=s.held_value,
                ),
            )
        )
    return out


def option_intents(
    plan_id: str, intents: Sequence[TradeIntent], config: TradingConfig
) -> List[TradeIntent]:
    """Scanner entries (entries.entry_intents) re-issued as plan items: covered
    calls first, then puts / spreads, at most ``max_option_items``."""
    order = {"covered_call": 0, "put_spread": 1, "index_put": 2, "stock_put": 3}
    ranked = sorted(intents, key=lambda i: order.get(i.strategy, 9))
    out = []
    for it in ranked[: config.rebuild.max_option_items]:
        out.append(
            replace(
                it,
                intent_id=_intent_id(plan_id, it.legs[0].contract.key),
                trigger="REBUILD",
                details=_plan_details(plan_id, config, **it.details),
            )
        )
    return out


def new_plan_id(request_id: str, now: Optional[datetime] = None) -> str:
    now = now or utcnow()
    return hashlib.sha256(f"{request_id}|{now.isoformat()}".encode()).hexdigest()[:12]
