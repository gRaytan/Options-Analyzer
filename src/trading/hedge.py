"""Crash hedge: a collar on the account (owner's choice, spec §5.12 revised).

Goal: no margin call in a 20% crash, and a much smaller loss in a 30% one.

* **Puts:** long XSP puts about ``put_otm`` (10%) below the index, 4-8 months
  out (``put_dte``), enough to cover ``coverage`` x the account's
  beta-weighted stock exposure (NBIS counts 2.5x, QQQ/QQQM 1.2x). In a crash
  they gain in cash at once, which raises Excess Liquidity just when the
  stocks lower it. XSP is cash-settled and can't be assigned.
* **Calls (collar):** covered calls about ``call_otm`` (8%) above on
  ``call_coverage`` (half) of the SPY / QQQM shares, same horizon. Their
  credit pays for most of the puts; the cost is that gains above +8% are
  capped on that half. (They use the covered-call capacity of those shares.)
* **Upkeep:** puts with ``roll_dte`` (60) days or fewer left no longer count:
  new ones are bought and the old ones sold for what's left of their value.
  Nothing is done while coverage is within ``min_shortfall`` of the target.

Every order waits for the owner's approval (like the rebuild plan), is asked
once a day, and goes through the risk gate. Margin restoration never sells
the hedge (it only buys back short options).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence, Tuple

from src.trading.broker.base import OptionChain
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
    make_intent_id,
)

STRATEGY = "tail_hedge"
INDEX_ROOTS = {"XSP", "SPX", "SPXW"}


def beta_exposure(positions: Sequence[Position], config: TradingConfig) -> float:
    """$ of S&P-equivalent long stock exposure (beta-weighted, at least 1x)."""
    total = 0.0
    for p in positions:
        c = p.contract
        if isinstance(c, StockContract) and p.quantity > 0:
            w = config.watchlist.get(c.symbol.upper())
            beta = max(w.beta if w and w.beta else 1.0, 1.0)
            total += p.quantity * (p.market_price or p.avg_cost) * beta
    return total


def hedge_puts(
    positions: Sequence[Position], config: TradingConfig, today: date
) -> Tuple[List[Position], List[Position]]:
    """(puts that count, puts to roll): long index puts, by days left."""
    keep, roll = [], []
    for p in positions:
        c = p.contract
        if (
            isinstance(c, OptionContract)
            and c.right is Right.PUT
            and p.quantity > 0
            and (c.trading_class or c.underlying).upper() in INDEX_ROOTS
        ):
            (
                keep if c.days_to_expiry(today) > config.tail_hedge.roll_dte else roll
            ).append(p)
    return keep, roll


def covered_notional(puts: Sequence[Position]) -> float:
    return sum(p.contract.strike * p.contract.multiplier * p.quantity for p in puts)


def _expiry(chain: OptionChain, today: date, dte: Tuple[int, int]) -> Optional[date]:
    lo, hi = dte
    inside = [e for e in chain.expiries if lo <= (e - today).days <= hi]
    if not inside:
        return None
    mid = (lo + hi) / 2
    return min(inside, key=lambda e: abs((e - today).days - mid))


def pick_put(
    chain: OptionChain, index_price: float, today: date, config: TradingConfig
):
    h = config.tail_hedge
    expiry = _expiry(chain, today, h.put_dte)
    below = [k for k in chain.strikes if k <= index_price * (1 - h.put_otm)]
    if expiry is None or not below:
        return None
    root = chain.trading_class or h.put_root
    return OptionContract(
        h.put_root,
        expiry,
        max(below),
        Right.PUT,
        chain.multiplier,
        ExerciseStyle.EUROPEAN,
        True,
        root,
    )


def pick_call(
    chain: OptionChain, symbol: str, price: float, today: date, config: TradingConfig
):
    h = config.tail_hedge
    expiry = _expiry(chain, today, h.put_dte)
    above = [k for k in chain.strikes if k >= price * (1 + h.call_otm)]
    if expiry is None or not above:
        return None
    return OptionContract(
        symbol,
        expiry,
        min(above),
        Right.CALL,
        chain.multiplier,
        ExerciseStyle.AMERICAN,
        False,
        chain.trading_class or symbol,
    )


@dataclass
class HedgeNeed:
    exposure: float
    target: float
    covered: float
    shortfall: float
    contracts: int
    roll: List[Position] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        return self.covered / self.exposure if self.exposure > 0 else 1.0


def need(
    positions: Sequence[Position],
    config: TradingConfig,
    index_price: float,
    today: date,
) -> HedgeNeed:
    h = config.tail_hedge
    exposure = beta_exposure(positions, config)
    keep, roll = hedge_puts(positions, config, today)
    covered = covered_notional(keep)
    target = h.coverage * exposure
    shortfall = max(0.0, target - covered)
    contracts = 0
    if exposure > 0 and shortfall > h.min_shortfall * target and index_price > 0:
        strike = index_price * (1 - h.put_otm)
        contracts = math.ceil(shortfall / (strike * 100))
    return HedgeNeed(exposure, target, covered, shortfall, contracts, roll)


def uncovered_call_lots(
    positions: Sequence[Position], symbol: str, share: float
) -> int:
    shares = sum(
        p.quantity
        for p in positions
        if isinstance(p.contract, StockContract)
        and p.contract.symbol == symbol
        and p.quantity > 0
    )
    short_calls = sum(
        abs(p.quantity)
        for p in positions
        if isinstance(p.contract, OptionContract)
        and p.contract.underlying == symbol
        and p.contract.right is Right.CALL
        and p.quantity < 0
    )
    return max(0, int(shares * share // 100) - int(short_calls))


def _details(config: TradingConfig, **extra) -> dict:
    return {
        "require_approval": True,
        "ask_once": True,
        "approval_ttl_s": config.rebuild.approval_hours * 3600,
        "hedge": True,
        **extra,
    }


def _intent(kind, legs, trigger, today, now, config, risk_reducing=False, **details):
    key = "|".join(f"{l.action.value}{l.contract.key}" for l in legs)
    return TradeIntent(
        make_intent_id(STRATEGY, kind, key, today),
        STRATEGY,
        kind,
        tuple(legs),
        trigger,
        now,
        risk_reducing,
        None,
        _details(config, **details),
    )


@dataclass
class HedgePlan:
    need: HedgeNeed
    intents: List[TradeIntent] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    put: Optional[OptionContract] = None
    put_cost: float = 0.0
    call_credit: float = 0.0

    def to_dict(self) -> dict:
        n = self.need
        return {
            "job": "hedge",
            "exposure": n.exposure,
            "target": n.target,
            "covered": n.covered,
            "coverage": n.coverage,
            "contracts": n.contracts,
            "roll": [p.contract.key for p in n.roll],
            "put": self.put.key if self.put else None,
            "put_cost": self.put_cost,
            "call_credit": self.call_credit,
            "intents": self.intents,
            "notes": self.notes,
        }


def plan(
    positions: Sequence[Position],
    account: AccountSnapshot,
    config: TradingConfig,
    index_price: float,
    put_chain: Optional[OptionChain],
    put_quotes: Dict[str, Quote],
    call_options: Dict[
        str, Tuple[OptionContract, Quote]
    ],  # symbol -> chosen call + quote
    today: date,
    now: datetime,
) -> HedgePlan:
    """The orders that bring the hedge to its target today (may be none)."""
    h = config.tail_hedge
    n = need(positions, config, index_price, today)
    out = HedgePlan(n)
    for old in n.roll:
        q = put_quotes.get(old.contract.key)
        out.intents.append(
            _intent(
                IntentKind.CLOSE,
                [OrderLeg(old.contract, Action.SELL, int(old.quantity))],
                "HEDGE_ROLL",
                today,
                now,
                config,
                worst_price=-round(max((q.bid or 0.0) if q else 0.0, 0.0), 2)
                if q
                else None,
                reason=f"{old.contract.days_to_expiry(today)} days left: sell what's left of its value",
            )
        )
    if n.contracts <= 0:
        out.notes.append(
            f"Hedge covers {n.coverage:.0%} of the stock exposure (target {h.coverage:.0%}): nothing to buy."
        )
    elif put_chain is None:
        out.notes.append(f"No {h.put_root} option chain: can't plan the puts.")
    else:
        put = pick_put(put_chain, index_price, today, config)
        q = put_quotes.get(put.key) if put else None
        if put is None or q is None or q.ask is None:
            out.notes.append(
                "No suitable put with a live quote in the 4-8 month window."
            )
        else:
            contracts = min(n.contracts, config.risk.max_contracts_xsp)
            cost = contracts * q.mid * put.multiplier
            out.put, out.put_cost = put, cost
            out.intents.append(
                _intent(
                    IntentKind.HEDGE,
                    [OrderLeg(put, Action.BUY, contracts)],
                    "HEDGE",
                    today,
                    now,
                    config,
                    worst_price=round(q.ask * 1.05, 2),
                    est_cost=cost,
                    reason=f"covers ${contracts * put.strike * 100:,.0f} of ${n.target:,.0f} beta-weighted stock",
                )
            )
            if contracts < n.contracts:
                out.notes.append(
                    f"Capped at {contracts} contracts per order; the rest next time."
                )
    if h.style == "collar" and out.put is not None:
        for sym, (call, q) in call_options.items():
            lots = uncovered_call_lots(positions, sym, h.call_coverage)
            if lots <= 0 or q is None or q.bid is None:
                continue
            credit = lots * q.mid * call.multiplier
            out.call_credit += credit
            out.intents.append(
                _intent(
                    IntentKind.HEDGE,
                    [OrderLeg(call, Action.SELL, lots)],
                    "HEDGE_COLLAR",
                    today,
                    now,
                    config,
                    worst_price=-round(q.bid * 0.95, 2),
                    est_credit=credit,
                    reason=f"covered calls on {lots * 100} of your {sym} shares pay for the puts",
                )
            )
    debit = out.put_cost - out.call_credit
    cap = h.max_debit_pct_nlv * account.net_liquidation
    if debit > cap and out.put is not None:
        out.notes.append(
            f"Net cost ${debit:,.0f} is above the {h.max_debit_pct_nlv:.0%} of NLV limit "
            f"(${cap:,.0f}): puts not planned today."
        )
        out.intents = [i for i in out.intents if i.trigger == "HEDGE_ROLL"]
        out.put, out.put_cost, out.call_credit = None, 0.0, 0.0
    return out
