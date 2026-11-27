"""Premium timing: is today's premium worth selling, and were past calls right?

Built from the journal's ``scan_results`` (every scan). Premium is the
**annual yield** of the best candidate per strategy and underlying (credit /
capital x 365 / DTE), which compares premium across days and expiries.

* :func:`daily_series`: one point per (strategy, underlying) per day: the
  day's best premium, and whether the tracker said **sell** (``fire``) at any
  scan that day.
* :func:`snapshot`: today's premium against its last 60 days (percentile,
  median, best) with the tracker's verdict.
* :func:`outcomes`: grades each past day once ``HORIZON`` later days exist.
  A **sell** day was right if no premium >= 10% richer came in the next 5
  days; a **wait** day was right if a premium at least as rich came later,
  and wrong if every later day was >= 10% cheaper (the chance was missed).
  The hit rate shows whether the timing rules earn their keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Dict, Iterable, List, Optional, Tuple

from src.trading.tracker import PREMIUM_DAYS, percentile_rank

HORIZON = 5  # trading days (scan days) to judge a call
BETTER = 1.10  # 10% richer
WORSE = 0.90  # 10% cheaper

Pair = Tuple[str, str]  # (strategy, underlying)


@dataclass
class Point:
    day: str
    premium: float  # best annual yield that day
    fire: bool  # the tracker said sell at some scan that day
    key: str = ""
    credit: float = 0.0
    delta: Optional[float] = None
    reasons: List[str] = field(default_factory=list)


def _underlying(key: str) -> str:
    return key.split(" ", 1)[0].upper()


def daily_series(records: Iterable[dict]) -> Dict[Pair, List[Point]]:
    """Per (strategy, underlying): the best premium per day, oldest first."""
    days: Dict[Pair, Dict[str, Point]] = {}
    for data in records:
        day = data.get("date")
        if not day:
            continue
        for c in data.get("best", []) + data.get("by_underlying", []):
            y, key = c.get("annual_yield"), c.get("key", "")
            if y is None or not key or not c.get("strategy"):
                continue
            pair = (c["strategy"], _underlying(key))
            by_day = days.setdefault(pair, {})
            p = by_day.get(day)
            fire = bool(c.get("fire"))
            if p is None or y > p.premium:
                by_day[day] = Point(
                    day,
                    float(y),
                    fire or (p.fire if p else False),
                    key,
                    float(c.get("net_credit") or c.get("credit") or 0.0),
                    c.get("delta"),
                    list(c.get("timing_reasons") or []),
                )
            elif fire:
                p.fire = True
    return {pair: [d[k] for k in sorted(d)] for pair, d in days.items()}


@dataclass
class Snapshot:
    strategy: str
    underlying: str
    today: Optional[Point]
    percentile: Optional[float]
    median: Optional[float]
    best: Optional[float]
    days: int

    @property
    def verdict(self) -> str:
        if self.today is None:
            return "no scan today"
        if self.today.fire:
            return "sell"
        return "wait"

    def to_dict(self) -> dict:
        t = self.today
        return {
            "strategy": self.strategy,
            "underlying": self.underlying,
            "verdict": self.verdict,
            "premium": t.premium if t else None,
            "key": t.key if t else None,
            "credit": t.credit if t else None,
            "delta": t.delta if t else None,
            "reasons": t.reasons if t else [],
            "percentile": self.percentile,
            "median": self.median,
            "best": self.best,
            "days": self.days,
        }


def snapshot(series: Dict[Pair, List[Point]], today: str) -> List[Snapshot]:
    out = []
    for (strategy, und), points in sorted(series.items()):
        past = [p.premium for p in points if p.day < today][-PREMIUM_DAYS:]
        now = next((p for p in points if p.day == today), None)
        out.append(
            Snapshot(
                strategy,
                und,
                now,
                percentile_rank(past, now.premium) if now and past else None,
                median(past) if past else None,
                max(past) if past else None,
                len(past),
            )
        )
    return out


@dataclass
class Outcome:
    strategy: str
    underlying: str
    day: str
    decision: str  # "sell" | "wait"
    premium: float
    later_best: float
    grade: str  # "right" | "wrong" | "even"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def outcomes(series: Dict[Pair, List[Point]], horizon: int = HORIZON) -> List[Outcome]:
    out = []
    for (strategy, und), points in series.items():
        for i, p in enumerate(points):
            later = points[i + 1 : i + 1 + horizon]
            if len(later) < horizon or p.premium <= 0:
                continue
            best = max(q.premium for q in later)
            if p.fire:
                grade = "wrong" if best >= p.premium * BETTER else "right"
            elif best >= p.premium:
                grade = "right"
            elif best < p.premium * WORSE:
                grade = "wrong"
            else:
                grade = "even"
            out.append(
                Outcome(
                    strategy,
                    und,
                    p.day,
                    "sell" if p.fire else "wait",
                    p.premium,
                    best,
                    grade,
                )
            )
    return sorted(out, key=lambda o: o.day, reverse=True)


def summary(results: List[Outcome]) -> dict:
    """Hit rate per decision: right / (right + wrong)."""
    out = {}
    for decision in ("sell", "wait"):
        rows = [o for o in results if o.decision == decision]
        right = sum(o.grade == "right" for o in rows)
        wrong = sum(o.grade == "wrong" for o in rows)
        out[decision] = {
            "count": len(rows),
            "right": right,
            "wrong": wrong,
            "hit_rate": right / (right + wrong) if right + wrong else None,
        }
    return out


def report(records: Iterable[dict], today: str) -> dict:
    series = daily_series(records)
    graded = outcomes(series)
    return {
        "today": today,
        "snapshot": [s.to_dict() for s in snapshot(series, today)],
        "summary": summary(graded),
        "outcomes": [o.to_dict() for o in graded[:60]],
    }
