"""Handling an assignment that happened anyway (plan T28c, spec §5.11, R7, R8).

Reconciliation (T13) recognizes an assignment when a short option shrinks or
disappears and the underlying's shares move by exactly the assigned amount.
For each one, :func:`handle_assignment`:

1. **Journals it**: synthetic fills close the option and add the delivered
   (or remove the called-away) shares, so the bot's expected positions match
   IB again; the roll chain is closed with an ``assigned`` note.
2. **Plans the undo**, per config:
   * put assigned -> ``on_put_assignment: sell_shares``: sell the delivered
     shares at the next open (a risk-reducing close of shares now held);
   * call assigned -> ``on_call_assignment: rebuy_shares``: buy the shares
     back at the next open, to stay fully invested (R8). The rebuy restores
     the pre-assignment holding, so the concentration caps don't block it;
     the margin checks (EL, Cushion) still apply.
   ``keep`` / ``keep_cash`` plan nothing.
3. Returns an alert text (critical).

Orders are sent by the engine once mode gating allows (T28), in market hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from src.trading.config import TradingConfig
from src.trading.domain import (
    Action,
    Fill,
    IntentKind,
    OrderLeg,
    Right,
    StockContract,
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.journal import Journal
from src.trading.reconcile import Assignment
from src.trading.rolls import ChainBook

STRATEGY = "assignment_handler"


@dataclass(frozen=True)
class AssignmentOutcome:
    assignment: Assignment
    intent: Optional[TradeIntent]
    message: str


def handle_assignment(
    a: Assignment,
    journal: Journal,
    config: TradingConfig,
    today: date,
    now: Optional[datetime] = None,
) -> AssignmentOutcome:
    now = now or utcnow()
    c = a.contract
    stock = StockContract(c.underlying, currency=c.currency)
    shares = int(abs(a.shares))
    ref = f"assignment-{c.key}-{today.isoformat()}"

    # 1. Synthetic fills so expected positions follow IB (idempotent by exec id).
    existing = {r.data.exec_id for r in journal.read("fills", key=ref)}
    synthetic = [
        Fill(
            f"{ref}-opt", ref, c, Action.BUY, a.contracts, 0.0, now
        ),  # short option gone
        Fill(
            f"{ref}-stk",
            ref,
            stock,
            Action.BUY if c.right is Right.PUT else Action.SELL,
            shares,
            c.strike,
            now,
        ),
    ]
    for fill in synthetic:
        if fill.exec_id not in existing:
            journal.append("fills", fill, key=ref)
    book = ChainBook(journal)
    chain = book.for_position(c.key)
    if chain is not None:
        book.record_close({**chain, "assigned": True}, c.strike)

    # 2. Plan the undo.
    intent = None
    g = config.guard
    if c.right is Right.PUT and g.on_put_assignment == "sell_shares":
        intent = TradeIntent(
            make_intent_id(STRATEGY, IntentKind.CLOSE, stock.key, today),
            STRATEGY,
            IntentKind.CLOSE,
            (OrderLeg(stock, Action.SELL, shares),),
            "PUT_ASSIGNED",
            now,
            True,
            details={"assigned_option": c.key, "strike": c.strike},
        )
        plan = f"sell the {shares} {c.underlying} shares at the next open"
    elif c.right is Right.CALL and g.on_call_assignment == "rebuy_shares":
        intent = TradeIntent(
            make_intent_id(STRATEGY, IntentKind.OPEN, stock.key, today),
            STRATEGY,
            IntentKind.OPEN,
            (OrderLeg(stock, Action.BUY, shares),),
            "CALL_ASSIGNED",
            now,
            False,
            details={
                "assigned_option": c.key,
                "strike": c.strike,
                "restore_assignment": shares,
            },
        )
        plan = f"buy back the {shares} {c.underlying} shares at the next open"
    else:
        plan = "keep as is (per config)"
    what = (
        "put assigned: received" if c.right is Right.PUT else "call assigned: delivered"
    )
    message = (
        f"{c.key} ({a.contracts} contracts) {what} {shares} {c.underlying} shares "
        f"at {c.strike:g}. Plan: {plan}."
    )
    journal.append("order_events", {"event": "assignment", "message": message}, key=ref)
    return AssignmentOutcome(a, intent, message)
