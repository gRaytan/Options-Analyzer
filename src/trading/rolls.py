"""Roll builder and roll chains (plan T28b, spec §5.11, R7).

When the guard (T28a) raises a roll trigger, the bot replaces the short
option with a later one **in one combo order** (buy back + sell new):

Candidates:
* expiries 30-60 DTE (never beyond ``roll_max_dte``, 90) that don't span
  earnings (single stocks);
* the same or a **better** strike (puts lower, calls higher);
* the same number of contracts (never doubling down);
* a net **credit** >= ``roll_min_credit`` ($0.05/contract) at mid;
* crash-scenario loss no higher than the current position's.

The best candidate is the one with the most strike improvement (then more
credit, then the nearer expiry). The order carries a hard worst price of
``-roll_min_credit``, so it can never fill for less than that credit.

**Close instead of rolling** when there's no qualifying candidate, the chain
has been rolled ``max_rolls`` (4) times, or the chain's cumulative loss
reached ``chain_max_loss_x_credit`` (3x) the original credit.

Roll chains link every roll to the original trade in the journal's
``chains`` table, so P&L and roll count are tracked per chain.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from src.trading.broker.base import OptionChain
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
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import Scenario, StressModel

STRATEGY = "assignment_guard"
MIN_ROLL_DTE = 30
TARGET_MAX_DTE = 60
MAX_STRIKES = 8  # candidate strikes per expiry (nearest better ones)


# -- roll chains ---------------------------------------------------------------


class ChainBook:
    """Roll history per original trade (append-only ``chains`` table)."""

    def __init__(self, journal: Journal):
        self.journal = journal

    def _all_latest(self) -> Dict[str, dict]:
        latest: Dict[str, dict] = {}
        for rec in self.journal.read("chains"):
            latest[rec.key] = rec.data
        return latest

    def for_position(self, key: str) -> Optional[dict]:
        for chain in self._all_latest().values():
            if chain["current_key"] == key and not chain.get("closed"):
                return chain
        return None

    def start(self, key: str, contracts: int, credit: float) -> dict:
        chain_id = hashlib.sha256(f"{key}|{utcnow().isoformat()}".encode()).hexdigest()[
            :12
        ]
        chain = {
            "chain_id": chain_id,
            "original_key": key,
            "current_key": key,
            "contracts": contracts,
            "original_credit": credit,  # per share
            "cumulative_credit": credit,  # per share, all rolls included
            "rolls": 0,
            "closed": False,
        }
        self.journal.append("chains", chain, key=chain_id)
        return chain

    def ensure(self, pos: Position) -> dict:
        return self.for_position(pos.contract.key) or self.start(
            pos.contract.key, int(abs(pos.quantity)), pos.avg_cost
        )

    def record_roll(self, chain: dict, new_key: str, net_credit: float) -> dict:
        updated = {
            **chain,
            "current_key": new_key,
            "cumulative_credit": chain["cumulative_credit"] + net_credit,
            "rolls": chain["rolls"] + 1,
        }
        self.journal.append("chains", updated, key=chain["chain_id"])
        return updated

    def record_close(self, chain: dict, cost: float) -> dict:
        closed = {**chain, "closed": True, "close_cost": cost}
        self.journal.append("chains", closed, key=chain["chain_id"])
        return closed


def chain_loss_multiple(chain: dict, current_mid: float) -> float:
    """Chain loss as a multiple of the original credit (0 = break-even or profit)."""
    loss = current_mid - chain["cumulative_credit"]
    return (
        max(0.0, loss) / chain["original_credit"]
        if chain["original_credit"] > 0
        else 0.0
    )


# -- candidates ----------------------------------------------------------------


@dataclass(frozen=True)
class RollCandidate:
    new: OptionContract
    net_credit: float  # per share, at mid (new mid - old mid)
    strike_gain: float  # how far the strike moved in our favor
    stress_before: float
    stress_after: float


@dataclass
class RollPlan:
    action: str  # "roll" | "close"
    reason: str
    intent: TradeIntent
    candidate: Optional[RollCandidate] = None
    considered: int = 0
    chain: dict = field(default_factory=dict)


def candidate_contracts(
    old: OptionContract,
    chains: Sequence[OptionChain],
    today: date,
    config: TradingConfig,
    events=None,
) -> List[OptionContract]:
    """Later expiries (30-60 DTE, max 90) with the same or better strikes."""
    g = config.guard
    tclass = old.trading_class or old.underlying
    chain = next((c for c in chains if c.trading_class == tclass), None)
    if chain is None and chains:
        chain = chains[0]
    if chain is None:
        return []
    expiries = [
        e
        for e in chain.expiries
        if e > old.expiry and MIN_ROLL_DTE <= (e - today).days <= g.roll_max_dte
    ]
    preferred = [e for e in expiries if (e - today).days <= TARGET_MAX_DTE]
    expiries = (preferred or expiries)[:4]
    if events is not None and old.can_be_assigned_early:
        expiries = [
            e
            for e in expiries
            if events.earnings_before(old.underlying, e, today) is False
        ]
    if old.right is Right.PUT:
        strikes = sorted((k for k in chain.strikes if k <= old.strike), reverse=True)[
            :MAX_STRIKES
        ]
    else:
        strikes = sorted(k for k in chain.strikes if k >= old.strike)[:MAX_STRIKES]
    return [
        OptionContract(
            old.underlying,
            e,
            k,
            old.right,
            old.multiplier,
            old.style,
            old.cash_settled,
            chain.trading_class,
            old.currency,
        )
        for e in expiries
        for k in strikes
    ]


def _stress_per_contract(
    contract: OptionContract,
    quote: Quote,
    account: AccountSnapshot,
    today: date,
    mm: float,
) -> float:
    pos = Position(contract, -1, quote.mid or 0.0, quote.mid)
    model = StressModel(
        exposures_from([pos], {contract.key: quote}),
        {contract.key: quote},
        account,
        today,
    )
    return max(0.0, -model.run(Scenario("budget", -0.20, 0.50, mm)).options_pnl)


def choose_roll(
    old: OptionContract,
    old_quote: Quote,
    candidates: Dict[str, Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
) -> Optional[RollCandidate]:
    g = config.guard
    mm = config.risk.stress_margin_multiplier
    if old_quote.mid is None:
        return None
    before = _stress_per_contract(old, old_quote, account, today, mm)
    good: List[RollCandidate] = []
    for q in candidates.values():
        new = q.contract
        if q.mid is None or q.delayed:
            continue
        credit = q.mid - old_quote.mid
        if credit < g.roll_min_credit:
            continue
        after = _stress_per_contract(new, q, account, today, mm)
        if after > before * (1 + 1e-6):
            continue  # a roll may not add crash risk
        gain = (
            (old.strike - new.strike)
            if old.right is Right.PUT
            else (new.strike - old.strike)
        )
        good.append(RollCandidate(new, credit, gain, before, after))
    if not good:
        return None
    good.sort(key=lambda c: (-c.strike_gain, -c.net_credit, c.new.expiry))
    return good[0]


# -- planning ------------------------------------------------------------------


def _close(
    pos: Position, reason: str, today: date, now: datetime, chain: dict
) -> RollPlan:
    c = pos.contract
    intent = TradeIntent(
        make_intent_id(STRATEGY, IntentKind.CLOSE, c.key, today),
        STRATEGY,
        IntentKind.CLOSE,
        (OrderLeg(c, Action.BUY, int(abs(pos.quantity))),),
        "GUARD_CLOSE",
        now,
        True,
        chain.get("chain_id"),
        {"reason": reason},
    )
    return RollPlan("close", reason, intent, chain=chain)


def plan_roll(
    pos: Position,
    old_quote: Quote,
    candidate_quotes: Dict[str, Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    chain: dict,
    today: date,
    trigger: str,
    now: Optional[datetime] = None,
) -> RollPlan:
    now = now or utcnow()
    g = config.guard
    c = pos.contract
    if chain["rolls"] >= g.max_rolls:
        return _close(
            pos, f"rolled {chain['rolls']} times (max {g.max_rolls})", today, now, chain
        )
    if (
        old_quote.mid is not None
        and chain_loss_multiple(chain, old_quote.mid) >= g.chain_max_loss_x_credit
    ):
        return _close(
            pos,
            f"chain loss reached {g.chain_max_loss_x_credit:g}x the original credit",
            today,
            now,
            chain,
        )
    best = choose_roll(c, old_quote, candidate_quotes, account, config, today)
    if best is None:
        return _close(
            pos,
            "no roll for a net credit at the same or a better strike",
            today,
            now,
            chain,
        )
    qty = int(abs(pos.quantity))
    intent = TradeIntent(
        make_intent_id(STRATEGY, IntentKind.ROLL, c.key, today),
        STRATEGY,
        IntentKind.ROLL,
        (OrderLeg(c, Action.BUY, qty), OrderLeg(best.new, Action.SELL, qty)),
        trigger.upper(),
        now,
        False,
        chain["chain_id"],
        {
            "worst_price": -g.roll_min_credit,  # never less than the minimum credit
            "expected_credit": best.net_credit,
            "strike_gain": best.strike_gain,
            "stress_before": best.stress_before,
            "stress_after": best.stress_after,
        },
    )
    return RollPlan(
        "roll",
        f"roll to {best.new.key} for {best.net_credit:.2f} credit",
        intent,
        best,
        len(candidate_quotes),
        chain,
    )


async def roll_for(
    broker,
    pos: Position,
    account: AccountSnapshot,
    config: TradingConfig,
    book: ChainBook,
    today: date,
    trigger: str,
    events=None,
    now: Optional[datetime] = None,
) -> Optional[RollPlan]:
    """Fetch the chain and quotes from the broker and plan the roll (or close)."""
    old = pos.contract
    [old_quote] = await broker.quotes([old])
    chains = await broker.option_chain(old.underlying)
    contracts = candidate_contracts(old, chains, today, config, events)
    quotes = (
        {q.contract.key: q for q in await broker.quotes(contracts)} if contracts else {}
    )
    return plan_roll(
        pos, old_quote, quotes, account, config, book.ensure(pos), today, trigger, now
    )
