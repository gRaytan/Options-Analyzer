"""Trend and volatility indicators (plan T29, spec §5.10).

Pure functions over daily bars (oldest first). The scanner and tracker
(T30/T31) use them to judge *when* to sell:

* trend: simple moving averages (20/50/200) and their slopes;
* momentum: RSI(14), ATR(14), distance from the 20-day high/low in ATRs;
* volatility: realized volatility (RV20/RV60, annualized), IV rank and IV
  percentile over a year of IV history, IV / RV edge;
* term structure (front vs. second month IV) and put/call skew.

Each function returns ``None`` when there isn't enough data, rather than a
misleading number.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import List, Optional, Sequence

from src.trading.domain import Bar

TRADING_DAYS = 252


def sma(values: Sequence[float], n: int) -> Optional[float]:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def sma_slope(values: Sequence[float], n: int, lookback: int = 5) -> Optional[float]:
    """Change of the n-day SMA over ``lookback`` days, as a fraction per day."""
    if len(values) < n + lookback:
        return None
    now, then = sma(values, n), sma(values[:-lookback], n)
    return (now - then) / then / lookback if then else None


def rsi(closes: Sequence[float], n: int = 14) -> Optional[float]:
    """Wilder's RSI."""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        change = b - a
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(bars: Sequence[Bar], n: int = 14) -> Optional[float]:
    """Wilder's Average True Range."""
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, bar in zip(bars[:-1], bars[1:]):
        trs.append(
            max(
                bar.high - bar.low,
                abs(bar.high - prev.close),
                abs(bar.low - prev.close),
            )
        )
    value = sum(trs[:n]) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def realized_vol(closes: Sequence[float], n: int) -> Optional[float]:
    """Annualized close-to-close volatility over the last n returns."""
    if len(closes) < n + 1:
        return None
    window = closes[-(n + 1) :]
    rets = [math.log(b / a) for a, b in zip(window[:-1], window[1:]) if a > 0 and b > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def iv_rank(ivs: Sequence[float], current: Optional[float] = None) -> Optional[float]:
    """(IV - 1y low) / (1y high - 1y low), 0-100."""
    if len(ivs) < 20:
        return None
    current = ivs[-1] if current is None else current
    lo, hi = min(ivs), max(ivs)
    if hi <= lo:
        return None
    return max(0.0, min(100.0, 100 * (current - lo) / (hi - lo)))


def iv_percentile(
    ivs: Sequence[float], current: Optional[float] = None
) -> Optional[float]:
    """Share of days in the history with IV below the current IV, 0-100."""
    if len(ivs) < 20:
        return None
    current = ivs[-1] if current is None else current
    return 100 * sum(1 for v in ivs if v < current) / len(ivs)


def distance_from_extreme_atr(bars: Sequence[Bar], n: int = 20) -> Optional[tuple]:
    """(from 20-day high, from 20-day low) in ATRs: how stretched the move is."""
    a = atr(bars)
    if a is None or len(bars) < n or a == 0:
        return None
    last = bars[-1].close
    hi = max(b.high for b in bars[-n:])
    lo = min(b.low for b in bars[-n:])
    return (hi - last) / a, (last - lo) / a


def term_structure(front_iv: float, second_iv: float) -> float:
    """Front minus second-month IV. Positive = inverted: stress or an event."""
    return front_iv - second_iv


def put_call_skew(put_iv: float, call_iv: float) -> float:
    """Put IV minus call IV at similar deltas. Higher = puts relatively rich."""
    return put_iv - call_iv


@dataclass(frozen=True)
class Indicators:
    close: float
    sma20: Optional[float]
    sma50: Optional[float]
    sma200: Optional[float]
    sma50_slope: Optional[float]
    rsi14: Optional[float]
    atr14: Optional[float]
    rv20: Optional[float]
    rv60: Optional[float]
    iv: Optional[float]
    iv_rank: Optional[float]
    iv_percentile: Optional[float]
    from_high_atr: Optional[float]
    from_low_atr: Optional[float]

    @property
    def vol_edge(self) -> Optional[float]:
        """IV / max(RV20, RV60): > 1 means options are priced above realized moves."""
        rv = max(v for v in (self.rv20, self.rv60, 0.0) if v is not None)
        return self.iv / rv if self.iv and rv > 0 else None

    @property
    def uptrend(self) -> Optional[bool]:
        if self.sma50 is None or self.sma50_slope is None:
            return None
        return self.close > self.sma50 and self.sma50_slope > 0

    def strong_uptrend(self, closes: Sequence[float]) -> Optional[bool]:
        """Spec §5.3: above a rising 50-day MA and a 20-day return > +8%."""
        if self.uptrend is None or len(closes) < 21:
            return None
        return bool(self.uptrend and closes[-1] / closes[-21] - 1 > 0.08)

    def to_dict(self) -> dict:
        return {**asdict(self), "vol_edge": self.vol_edge}


def compute(
    bars: Sequence[Bar], iv_history: Sequence[float] = ()
) -> Optional[Indicators]:
    """All indicators from daily price bars and (optionally) daily IV history."""
    if not bars:
        return None
    closes: List[float] = [b.close for b in bars]
    stretch = distance_from_extreme_atr(bars)
    iv_now = iv_history[-1] if iv_history else None
    return Indicators(
        close=closes[-1],
        sma20=sma(closes, 20),
        sma50=sma(closes, 50),
        sma200=sma(closes, 200),
        sma50_slope=sma_slope(closes, 50),
        rsi14=rsi(closes),
        atr14=atr(bars),
        rv20=realized_vol(closes, 20),
        rv60=realized_vol(closes, 60),
        iv=iv_now,
        iv_rank=iv_rank(iv_history) if iv_history else None,
        iv_percentile=iv_percentile(iv_history) if iv_history else None,
        from_high_atr=stretch[0] if stretch else None,
        from_low_atr=stretch[1] if stretch else None,
    )
