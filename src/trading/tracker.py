"""Opportunity tracker: when to sell (plan T31, spec §5.10, R4).

The scanner (T30) finds candidates that are acceptable *now*. The tracker
decides whether *now* is a good moment, from signals that make premium
richer than usual:

| Signal | Covered calls | Puts | Effect |
|---|---|---|---|
| IV rank >= 50 or IV percentile in the top third | yes | yes | +0.10 |
| The underlying stretched the right way: >= 1 ATR in 1-3 days, or within 0.5 ATR of the 20-day extreme | up (high) | down (low) | +0.10 |
| Today's best score in the top quartile of its own 20-day history | yes | yes | +0.10 |
| ... in the bottom quartile | yes | yes | -0.10 |
| VIX spike fading (regime ``fading``) | no | index puts and put spreads | +0.20 |

The multiplier is clamped to 0.8-1.3 and scales the candidate's score.
A candidate is **ready to sell** when its multiplier is >= ``FIRE_AT`` (1.1),
meaning at least one supporting signal and nothing against it. There are
**no forced sales**: if timing never fires, nothing is sold (spec §5.10).

History comes from the journal's ``scan_results`` (the best score per
underlying per day, from every scan).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from src.trading.config import StrategyName
from src.trading.indicators import Indicators, atr
from src.trading.journal import Journal

FIRE_AT = 1.10
LOW, HIGH = 0.8, 1.3
HISTORY_DAYS = 20


@dataclass(frozen=True)
class Timing:
    multiplier: float
    reasons: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def fire(self) -> bool:
        return self.multiplier >= FIRE_AT - 1e-9


def _quartiles(values: Sequence[float]) -> Optional[tuple]:
    if len(values) < 5:
        return None
    s = sorted(values)
    q1 = s[int(0.25 * (len(s) - 1))]
    q3 = s[int(0.75 * (len(s) - 1))]
    return q1, q3


class Tracker:
    def __init__(self, journal: Journal):
        self.journal = journal

    def daily_best(
        self, strategy: StrategyName, underlying: str, today: date
    ) -> List[float]:
        """Best score per day for this strategy and underlying, last 20 days before today."""
        start = today - timedelta(days=int(HISTORY_DAYS * 1.5))  # ~20 trading days
        best: Dict[str, float] = {}
        for rec in self.journal.read("scan_results"):
            data = rec.data
            day = data.get("date", "")
            if not day or day >= today.isoformat() or day < start.isoformat():
                continue
            for c in data.get("best", []):
                if c.get("strategy") == strategy.value and c.get("key", "").startswith(
                    underlying.upper() + " "
                ):
                    raw = c.get("raw_score", c.get("score", 0.0))
                    best[day] = max(best.get(day, 0.0), raw)
        return [best[d] for d in sorted(best)][-HISTORY_DAYS:]

    def timing(
        self,
        strategy: StrategyName,
        underlying: str,
        ind: Optional[Indicators],
        bars: Sequence,
        regime: str,
        best_score_today: Optional[float],
        today: date,
    ) -> Timing:
        mult, reasons = 1.0, []
        if ind is not None:
            if (ind.iv_rank or 0) >= 50 or (ind.iv_percentile or 0) >= 200 / 3:
                mult += 0.10
                reasons.append("IV high vs its year")
            calls = strategy is StrategyName.COVERED_CALL
            a = atr(bars) if bars else None
            stretched = False
            if a is not None and a > 0 and len(bars) >= 4:
                moves = [bars[-1].close - bars[-1 - k].close for k in (1, 2, 3)]
                # Calls want the stock up, puts want it down: signed in that direction.
                signed = moves if calls else [-m for m in moves]
                stretched = any(m >= a for m in signed)
            near = ind.from_high_atr if calls else ind.from_low_atr
            if stretched or (near is not None and near <= 0.5):
                mult += 0.10
                reasons.append(
                    "stock just rose (call premium richer)"
                    if calls
                    else "stock just fell (put premium richer)"
                )
        history = self.daily_best(strategy, underlying, today)
        q = _quartiles(history)
        if q is not None and best_score_today is not None:
            if best_score_today >= q[1]:
                mult += 0.10
                reasons.append("premium in the top quartile of 20 days")
            elif best_score_today <= q[0]:
                mult -= 0.10
                reasons.append("premium in the bottom quartile of 20 days")
        if (
            strategy in (StrategyName.INDEX_PUT, StrategyName.PUT_SPREAD)
            and regime == "fading"
        ):
            mult += 0.20
            reasons.append("VIX spike fading")
        return Timing(max(LOW, min(HIGH, mult)), tuple(reasons))
