"""Spike watch: catch rich-premium moments between scans (real time, ~60 s).

Premium is richest in the first minutes of a sharp move or a VIX jump, and
the full scan runs every 15 minutes. Every minute in market hours the engine
quotes only the underlyings (a few quotes, not option chains) and VIX, and
looks for:

| Trigger | Means | What it does |
|---|---|---|
| ``up``: a stock >= ``ATR_TRIGGER`` (1) ATR above yesterday's close | call premium richer | scan now (covered calls); for shares at another broker, a phone alert with the call to sell |
| ``down``: the index root / SPY >= 1 ATR below yesterday's close | put premium richer | scan now (puts / put spreads) |
| ``vix_jump``: VIX >= 10% above yesterday's close | all premium richer | scan now |
| ``vix_fading``: after a jump, VIX >= 5% off today's high | the spike is fading: often the best put-spread entry | scan now |

Each (symbol, trigger) fires at most once per ``COOLDOWN`` (30 min); every
firing is journaled (``spike_events``) so the Premium timing page can show
them and the hit rate can be judged over time. The scan's own rules (timing,
premium percentile, the risk gate) still decide whether anything is sold.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from src.trading.journal import Journal

ATR_TRIGGER = 1.0
VIX_JUMP = 0.10
VIX_FADE = 0.05
COOLDOWN = timedelta(minutes=30)
TABLE = "spike_events"

UP, DOWN, VIX_UP, VIX_FADING = "up", "down", "vix_jump", "vix_fading"


@dataclass(frozen=True)
class Spike:
    symbol: str
    kind: str
    price: float
    ref: float  # yesterday's close (or today's VIX high for vix_fading)
    size: float  # ATRs for up/down, fraction for VIX
    detail: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def stock_spikes(
    symbol: str,
    price: Optional[float],
    prev_close: Optional[float],
    atr: Optional[float],
    want_up: bool,
    want_down: bool,
) -> List[Spike]:
    if not price or not prev_close or not atr or atr <= 0:
        return []
    move = price - prev_close
    atrs = move / atr
    pct = move / prev_close
    out = []
    if want_up and atrs >= ATR_TRIGGER:
        out.append(
            Spike(
                symbol,
                UP,
                price,
                prev_close,
                atrs,
                f"{symbol} {pct:+.1%} today ({atrs:.1f} ATR): call premium richer",
            )
        )
    if want_down and -atrs >= ATR_TRIGGER:
        out.append(
            Spike(
                symbol,
                DOWN,
                price,
                prev_close,
                -atrs,
                f"{symbol} {pct:+.1%} today ({-atrs:.1f} ATR): put premium richer",
            )
        )
    return out


def vix_spikes(
    vix: Optional[float], prev_close: Optional[float], day_high: Optional[float]
) -> List[Spike]:
    if not vix or not prev_close:
        return []
    out = []
    jump = vix / prev_close - 1
    if jump >= VIX_JUMP:
        out.append(
            Spike(
                "VIX",
                VIX_UP,
                vix,
                prev_close,
                jump,
                f"VIX {vix:.1f}, {jump:+.0%} today: option premium richer",
            )
        )
    high = max(day_high or vix, vix)
    if high / prev_close - 1 >= VIX_JUMP and vix <= high * (1 - VIX_FADE):
        off = 1 - vix / high
        out.append(
            Spike(
                "VIX",
                VIX_FADING,
                vix,
                high,
                off,
                f"VIX {vix:.1f}, {off:.0%} off today's high {high:.1f}: "
                "spike fading (put spreads)",
            )
        )
    return out


class SpikeWatch:
    """Rate limit and journal for spikes; remembers VIX's high for the day."""

    def __init__(self, journal: Journal, cooldown: timedelta = COOLDOWN):
        self.journal = journal
        self.cooldown = cooldown
        self._vix_high: Tuple[Optional[date], float] = (None, 0.0)

    def vix_high(self, today: date, vix: Optional[float]) -> Optional[float]:
        day, high = self._vix_high
        if day != today:
            high = 0.0
        if vix:
            high = max(high, vix)
        self._vix_high = (today, high)
        return high or None

    def _last_fired(self) -> Dict[Tuple[str, str], datetime]:
        out: Dict[Tuple[str, str], datetime] = {}
        for r in self.journal.read(TABLE):
            d = r.data
            at = datetime.fromisoformat(d["at"])
            k = (d["symbol"], d["kind"])
            if k not in out or at > out[k]:
                out[k] = at
        return out

    def new(self, spikes: Iterable[Spike], now: datetime) -> List[Spike]:
        """The spikes not fired within the cooldown; journals them as fired."""
        last = self._last_fired()
        fresh = []
        for s in spikes:
            at = last.get((s.symbol, s.kind))
            if at is not None and now - at < self.cooldown:
                continue
            fresh.append(s)
            self.journal.append(
                TABLE,
                {**s.to_dict(), "at": now.isoformat(), "date": now.date().isoformat()},
                key=f"{s.symbol}:{s.kind}",
            )
        return fresh
