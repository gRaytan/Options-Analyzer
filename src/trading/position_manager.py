"""Position manager: when to close a short option early (plan T25, spec §5.8, R2).

The default is to **hold to expiration**: an option that expires worthless
costs no commission. A short option is closed early only when one of these
rules says it pays (each produces a risk-reducing CLOSE intent):

| Rule | Closes when |
|---|---|
| ``stop_loss`` | the option costs >= (1 + ``chain_max_loss_x_credit``) x the credit, i.e. the loss reached 3x the credit (§5.11) |
| ``gamma_exit`` | DTE <= 10, the remaining time value < 20% of the credit, and abs(delta) > 0.25: little left to earn, risk rising |
| ``replacement`` | >= 50% of the credit captured, **and** the remaining premium per day per unit of crash loss is < 50% of the best new candidate's, **and** closing + reopening fees < 10% of the new credit |

Never buys back an option at mid <= $0.05 (``never_buy_back_below``):
leaving it to expire is cheaper. Rolls and assignment avoidance are the
guards' job (T28a/b); margin restoration is T26.

Only **short** options are managed, and only those the bot manages (its own
positions, or ones the owner opted in): the owner's positions are left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Set

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Quote,
    TradeIntent,
    make_intent_id,
    utcnow,
)
from src.trading.fees import FeeCalibrator, estimate_order_fees
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import Scenario, StressModel

STRATEGY = "position_manager"


@dataclass(frozen=True)
class Candidate:
    """The best new trade the scanner could put the freed risk into."""

    efficiency: float  # premium per day per $ of crash loss
    credit: float  # $ credit for one contract (after fees)


def efficiency(
    premium_per_share: float, multiplier: int, days: int, stress_loss: float
) -> float:
    """Premium per day per dollar of crash-scenario loss (the T30 ranking unit)."""
    if days <= 0 or stress_loss <= 0:
        return 0.0
    return premium_per_share * multiplier / days / stress_loss


@dataclass(frozen=True)
class Decision:
    key: str
    rule: str
    detail: str
    intent: TradeIntent


class PositionManager:
    def __init__(
        self, config: TradingConfig, calibrator: Optional[FeeCalibrator] = None
    ):
        self.config = config
        self.calibrator = calibrator

    def _stress_loss_per_contract(
        self, pos: Position, quote: Quote, account: AccountSnapshot, today: date
    ) -> float:
        one = Position(pos.contract, -1, pos.avg_cost, quote.mid)
        exp = exposures_from([one], {pos.contract.key: quote})
        model = StressModel(exp, {pos.contract.key: quote}, account, today)
        mm = self.config.risk.stress_margin_multiplier
        return max(0.0, -model.run(Scenario("budget", -0.20, 0.50, mm)).options_pnl)

    def _close_intent(
        self, pos: Position, rule: str, today: date, now: datetime, **details
    ) -> TradeIntent:
        c: OptionContract = pos.contract
        return TradeIntent(
            intent_id=make_intent_id(STRATEGY, IntentKind.CLOSE, c.key, today),
            strategy=STRATEGY,
            kind=IntentKind.CLOSE,
            legs=(OrderLeg(c, Action.BUY, int(abs(pos.quantity))),),
            trigger=rule.upper(),
            created_at=now,
            risk_reducing=True,
            details=details,
        )

    def review(
        self,
        positions: Iterable[Position],
        quotes: Dict[str, Quote],
        account: AccountSnapshot,
        managed: Set[str],
        today: date,
        best_candidate: Optional[Candidate] = None,
        now: Optional[datetime] = None,
    ) -> List[Decision]:
        """Close decisions for the managed short options. Empty list = hold."""
        now = now or utcnow()
        ex, guard = self.config.exit, self.config.guard
        out: List[Decision] = []
        for pos in positions:
            c = pos.contract
            if (
                not isinstance(c, OptionContract)
                or pos.quantity >= 0
                or c.key not in managed
            ):
                continue
            q = quotes.get(c.key)
            if q is None or q.mid is None:
                continue  # no market: nothing sensible to decide
            credit, mid = pos.avg_cost, q.mid
            dte = c.days_to_expiry(today)
            delta = abs(q.greeks.delta) if q.greeks else None

            # Stop loss: the loss reached chain_max_loss_x_credit x the credit.
            if credit > 0 and mid - credit >= guard.chain_max_loss_x_credit * credit:
                out.append(
                    self._decide(
                        pos,
                        "stop_loss",
                        today,
                        now,
                        f"cost {mid:.2f} = {mid / credit:.1f}x the {credit:.2f} credit",
                        mid=mid,
                        credit=credit,
                    )
                )
                continue

            if mid <= ex.never_buy_back_below:
                continue  # let it expire: buying back costs more than it saves

            # Gamma exit: little time value left, risk rising, near expiry.
            extrinsic = q.extrinsic()
            ge = ex.gamma_exit
            if (
                dte <= ge.dte
                and credit > 0
                and extrinsic is not None
                and extrinsic < ge.max_remaining_extrinsic * credit
                and delta is not None
                and delta > ge.min_delta
            ):
                out.append(
                    self._decide(
                        pos,
                        "gamma_exit",
                        today,
                        now,
                        f"{dte} DTE, time value {extrinsic:.2f} < "
                        f"{ge.max_remaining_extrinsic:.0%} of credit, delta {delta:.2f}",
                        dte=dte,
                        extrinsic=extrinsic,
                        delta=delta,
                    )
                )
                continue

            # Replacement test: redeploy the risk into a clearly better trade.
            rt = ex.replacement_test
            captured = (credit - mid) / credit if credit > 0 else 0.0
            if best_candidate is None or captured < rt.min_captured or dte <= 0:
                continue
            stress = self._stress_loss_per_contract(pos, q, account, today)
            current_eff = efficiency(mid, c.multiplier, dte, stress)
            if current_eff >= rt.rel_efficiency * best_candidate.efficiency:
                continue
            probe = OrderRequest("probe", (OrderLeg(c, Action.BUY, 1),), mid, "probe")
            close_fees = estimate_order_fees(
                probe, {c.key: mid}, self.config.fees, self.calibrator
            ).total
            reopen_fees = close_fees  # same size order
            if close_fees + reopen_fees >= rt.max_fee_ratio * best_candidate.credit:
                continue
            out.append(
                self._decide(
                    pos,
                    "replacement",
                    today,
                    now,
                    f"{captured:.0%} captured; efficiency {current_eff:.2e} < "
                    f"{rt.rel_efficiency:.0%} of the best candidate's "
                    f"{best_candidate.efficiency:.2e}",
                    captured=captured,
                    efficiency=current_eff,
                )
            )
        return out

    def _decide(self, pos, rule, today, now, detail, **details) -> Decision:
        intent = self._close_intent(pos, rule, today, now, detail=detail, **details)
        return Decision(pos.contract.key, rule, detail, intent)
