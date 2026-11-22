"""Reconciliation: does IB hold what the bot thinks it holds? (plan T13, spec §7.4)

Expected positions come from the journal: the sum of the bot's own fills
per contract. Each cycle they're compared with IB's positions:

* **match**: the bot-managed quantity equals IB's.
* **unmanaged**: IB holds something the bot never traded (your own or
  legacy positions). Not an error: shown, monitored, not traded (spec §5.7).
* **expired**: a bot-managed option past its expiry date is gone. Explained.
* **mismatch**: a bot-managed quantity differs from IB with no explanation
  (drift, a missing position, a possible assignment). Trading must halt
  until a human acknowledges it (§6.4); the halt itself is wired in T15.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from src.trading.domain import Fill, OptionContract, Position, Right, StockContract
from src.trading.journal import Journal

QTY_TOLERANCE = 1e-9


@dataclass(frozen=True)
class Mismatch:
    key: str
    expected: float
    actual: float
    hint: str = ""


@dataclass(frozen=True)
class ReconcileResult:
    matched: Tuple[str, ...] = ()
    unmanaged: Tuple[str, ...] = ()
    expired: Tuple[str, ...] = ()
    mismatches: Tuple[Mismatch, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.mismatches

    @property
    def should_halt(self) -> bool:
        return bool(self.mismatches)


def expected_from_fills(fills: Iterable[Fill]) -> Dict[str, Tuple[object, float]]:
    """Signed net quantity per contract key from the bot's fills."""
    totals: Dict[str, float] = defaultdict(float)
    contracts: Dict[str, object] = {}
    for fill in fills:
        totals[fill.contract.key] += fill.action.sign * fill.quantity
        contracts[fill.contract.key] = fill.contract
    return {
        key: (contracts[key], qty)
        for key, qty in totals.items()
        if abs(qty) > QTY_TOLERANCE
    }


def expected_from_journal(journal: Journal) -> Dict[str, Tuple[object, float]]:
    return expected_from_fills(record.data for record in journal.read("fills"))


def _assigned_at_expiry(
    contract: OptionContract,
    exp_qty: float,
    actual_qty: Dict[str, float],
    previous: Optional[Dict[str, float]],
) -> bool:
    """Did shares move the way an assignment/exercise of this option would?

    Physically settled options that vanish at expiry either expired worthless
    or were assigned. Assignment shows up as a share change in the underlying
    since the previous snapshot: short put / long call -> shares up;
    short call / long put -> shares down.
    """
    if contract.cash_settled or previous is None:
        return False
    und = contract.underlying
    change = actual_qty.get(und, 0.0) - previous.get(und, 0.0)
    receives_shares = (contract.right is Right.PUT) == (exp_qty < 0)
    return change > QTY_TOLERANCE if receives_shares else change < -QTY_TOLERANCE


def reconcile(
    expected: Dict[str, Tuple[object, float]],
    actual: Iterable[Position],
    today: date,
    previous: Optional[Dict[str, float]] = None,
) -> ReconcileResult:
    """``previous``: contract key -> quantity from the last snapshot, used to
    tell an expiry from an assignment at expiry (validation finding 4)."""
    actual_qty: Dict[str, float] = defaultdict(float)
    for pos in actual:
        actual_qty[pos.contract.key] += pos.quantity

    matched: List[str] = []
    expired: List[str] = []
    mismatches: List[Mismatch] = []
    unmanaged: List[str] = []

    for key, (contract, exp_qty) in sorted(expected.items()):
        act_qty = actual_qty.get(key, 0.0)
        if isinstance(contract, StockContract):
            # Shares are mostly the owner's portfolio. The bot's own shares (e.g.
            # from an assignment it is handling) must still be there; more is fine.
            ok = (exp_qty > 0 and act_qty >= exp_qty - QTY_TOLERANCE) or (
                exp_qty < 0 and act_qty <= exp_qty + QTY_TOLERANCE
            )
            if ok:
                matched.append(key)
            else:
                mismatches.append(
                    Mismatch(key, exp_qty, act_qty, "bot-traded shares no longer held")
                )
            continue
        if abs(act_qty - exp_qty) <= QTY_TOLERANCE:
            matched.append(key)
        elif (
            act_qty == 0
            and isinstance(contract, OptionContract)
            and contract.expiry < today
        ):
            if _assigned_at_expiry(contract, exp_qty, actual_qty, previous):
                mismatches.append(
                    Mismatch(key, exp_qty, act_qty, "assigned or exercised at expiry")
                )
            else:
                expired.append(key)
        else:
            hint = ""
            if (
                isinstance(contract, OptionContract)
                and exp_qty < 0
                and abs(act_qty) < abs(exp_qty)
            ):
                hint = "short option reduced or gone before expiry: possible assignment"
            elif act_qty == 0:
                hint = "position missing at IB"
            else:
                hint = "quantity differs from the bot's fills"
            mismatches.append(Mismatch(key, exp_qty, act_qty, hint))

    for key in sorted(actual_qty):
        if key not in expected and abs(actual_qty[key]) > QTY_TOLERANCE:
            unmanaged.append(key)

    return ReconcileResult(
        matched=tuple(matched),
        unmanaged=tuple(unmanaged),
        expired=tuple(expired),
        mismatches=tuple(mismatches),
    )


def result_to_dict(result: ReconcileResult) -> dict:
    """Plain JSON form for the journal and the status page."""
    return {
        "ok": result.ok,
        "matched": list(result.matched),
        "unmanaged": list(result.unmanaged),
        "expired": list(result.expired),
        "mismatches": [
            {"key": m.key, "expected": m.expected, "actual": m.actual, "hint": m.hint}
            for m in result.mismatches
        ],
    }
