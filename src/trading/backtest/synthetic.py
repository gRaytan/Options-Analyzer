"""Synthetic option chains from free data (plan T36, spec §11.1).

No historical option data is needed. Each day's chain is priced from:

1. **ATM implied vol by DTE.** SPX family (SPY, XSP, SPX): VIX9D (9 days),
   VIX (30), VIX3M (93), interpolated in total variance and flat outside.
   QQQ/QQQM: VXN at 30 days, shaped by the same term structure. VIX is a
   variance-swap level that already includes skew, so ATM IV is taken as
   ``atm_ratio`` (0.90) x the index. Missing series fall back to VIX
   (VXN to VIX x 1.25), flagged in ``SyntheticMarket.fallbacks``.
2. **A conservative put skew.** With z = ln(K/F) / (ATM IV x sqrt(t)) the
   standard moves out of the money:

   * puts (z < 0): IV x (1 + slope x (-z) + curve x z^2), slope steeper when
     VIX is low (x sqrt(20 / VIX), clamped 0.7-1.4);
   * calls (z > 0): IV x max(0.80, 1 - call_slope x z).

   The defaults put a 10-delta SPX put about 3 vol points over ATM at VIX
   20, **less** than the real market (typically 6-8): premium received by a
   put seller is understated, which errs on the safe side. The first weeks
   of recorded IB chains calibrate it (``scripts/backtest_data.py compare``).
3. **Black-Scholes-Merton** with a flat rate and a dividend yield per symbol.
4. **A pessimistic bid-ask:** 5% of mid, at least $0.05 wide.

Expiries are the Fridays of the next 9 weeks plus the third Fridays out to
120 days (a Friday holiday moves to Thursday). Strikes: $1 within 15% of
the price, $5 beyond, out to 40% (SPX: $5 / $25).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from py_vollib.black_scholes_merton import black_scholes_merton
from py_vollib.black_scholes_merton.greeks.analytical import (
    delta as bsm_delta,
    gamma as bsm_gamma,
    theta as bsm_theta,
    vega as bsm_vega,
)

from src.trading.backtest.data import MarketHistory
from src.trading.broker.base import OptionChain
from src.trading.domain import (
    Bar,
    ExerciseStyle,
    Greeks,
    OptionContract,
    Quote,
    Right,
)

SPX_FAMILY = {"SPY", "XSP", "SPX"}
NDX_FAMILY = {"QQQ", "QQQM"}
INDEX_ROOTS = {"XSP", "SPX"}
TERM_POINTS = (("VIX9D", 9), ("VIX", 30), ("VIX3M", 93))


@dataclass(frozen=True)
class SyntheticParams:
    rate: float = 0.04
    atm_ratio: float = 0.90
    skew_slope: float = 0.10
    skew_curve: float = 0.02
    call_slope: float = 0.06
    spread_pct: float = 0.05
    min_width: float = 0.05
    vxn_fallback_ratio: float = 1.25
    dividend_yield: Dict[str, float] = field(
        default_factory=lambda: {
            "SPY": 0.013,
            "XSP": 0.013,
            "SPX": 0.013,
            "QQQ": 0.006,
            "QQQM": 0.006,
        }
    )


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


_CALENDAR = None


def _is_session(day: date) -> bool:
    """NYSE session? (exchange_calendars; weekdays only if it's unavailable)."""
    global _CALENDAR  # pylint: disable=global-statement
    if _CALENDAR is None:
        try:
            from src.trading.calendars import (  # pylint: disable=import-outside-toplevel
                MarketCalendar,
            )

            _CALENDAR = MarketCalendar()
        except Exception:  # pylint: disable=broad-except
            _CALENDAR = False
    if _CALENDAR is False:
        return day.weekday() < 5
    try:
        return _CALENDAR.is_trading_day(day)
    except Exception:  # pylint: disable=broad-except  # outside the calendar's range
        return day.weekday() < 5


def expiries(day: date) -> List[date]:
    """Weekly Fridays (9 weeks) + monthly third Fridays to 120 days, after ``day``.

    A Friday holiday (e.g. Good Friday, Christmas) moves to the Thursday."""
    out = set()
    friday = day + timedelta(days=(4 - day.weekday()) % 7 or 7)
    for k in range(9):
        out.add(friday + timedelta(weeks=k))
    y, m = day.year, day.month
    for _ in range(5):
        tf = _third_friday(y, m)
        if day < tf <= day + timedelta(days=120):
            out.add(tf)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    result = set()
    for e in out:
        while not _is_session(e) and e > day:
            e -= timedelta(days=1)
        if e > day:
            result.add(e)
    return sorted(result)


def strikes(price: float, symbol: str) -> Tuple[float, ...]:
    fine, coarse = (5.0, 25.0) if symbol.upper() == "SPX" else (1.0, 5.0)
    out = set()
    k = math.floor(price * 0.60 / coarse) * coarse
    while k <= price * 1.40:
        step = fine if abs(k / price - 1) <= 0.15 else coarse
        if k > 0 and abs(k / step - round(k / step)) < 1e-9:
            out.add(round(k, 2))
        k += fine
    return tuple(sorted(out))


class SyntheticMarket:
    """Prices any option on any day of the loaded history."""

    def __init__(
        self, history: MarketHistory, params: SyntheticParams = SyntheticParams()
    ):
        self.history = history.with_xsp()
        self.p = params
        self._closes = {s: self.history.closes(s) for s in self.history.bars}
        self.fallbacks: Set[str] = set()
        self.day: Optional[date] = None  # the "today" used by quote_source

    # -- inputs ---------------------------------------------------------------

    def price(self, symbol: str, day: date) -> Optional[float]:
        return self._closes.get(symbol.upper(), {}).get(day)

    def _spx_term(self, day: date, dte: int) -> Optional[float]:
        """SPX-family index level for ``dte`` days (fraction), from the VIX term points."""
        vix = self.history.vol_on("VIX", day)
        if vix is None:
            return None
        pts = []
        for name, d in TERM_POINTS:
            v = self.history.vol_on(name, day)
            if v is None:
                self.fallbacks.add(f"{name} missing: VIX used")
                v = vix
            pts.append((d, v))
        t = max(dte, 1)
        if t <= pts[0][0]:
            return pts[0][1]
        if t >= pts[-1][0]:
            return pts[-1][1]
        for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
            if d0 <= t <= d1:
                w0, w1 = v0 * v0 * d0, v1 * v1 * d1  # total variance
                w = w0 + (w1 - w0) * (t - d0) / (d1 - d0)
                return math.sqrt(w / t)
        return vix

    def atm_iv(self, symbol: str, day: date, dte: int) -> Optional[float]:
        sym = symbol.upper()
        spx = self._spx_term(day, dte)
        if spx is None:
            return None
        if sym in SPX_FAMILY:
            level = spx
        elif sym in NDX_FAMILY:
            vix = self.history.vol_on("VIX", day)
            vxn = self.history.vol_on("VXN", day)
            if vxn is None:
                self.fallbacks.add("VXN missing: VIX x 1.25 used")
                vxn = vix * self.p.vxn_fallback_ratio
            level = vxn * spx / vix
        else:
            return None  # single stocks: no IV history (spec §11.1)
        return max(0.03, level * self.p.atm_ratio)

    def skewed_iv(
        self, strike: float, forward: float, atm: float, t: float, vix: float
    ) -> float:
        z = math.log(strike / forward) / (atm * math.sqrt(max(t, 1 / 365)))
        if z < 0:
            steep = min(1.4, max(0.7, math.sqrt(0.20 / max(vix, 0.05))))
            mult = 1 + self.p.skew_slope * steep * (-z) + self.p.skew_curve * z * z
        else:
            mult = max(0.80, 1 - self.p.call_slope * z)
        return max(0.03, atm * mult)

    # -- chains and quotes ----------------------------------------------------------

    def contract(
        self, symbol: str, expiry: date, strike: float, right: Right
    ) -> OptionContract:
        sym = symbol.upper()
        index = sym in INDEX_ROOTS
        return OptionContract(
            sym,
            expiry,
            strike,
            right,
            100,
            ExerciseStyle.EUROPEAN if index else ExerciseStyle.AMERICAN,
            index,
            sym,
        )

    def chain(self, symbol: str, day: date) -> List[OptionChain]:
        price = self.price(symbol, day)
        if price is None:
            return []
        sym = symbol.upper()
        return [OptionChain(sym, sym, 100, tuple(expiries(day)), strikes(price, sym))]

    def quote(
        self, contract: OptionContract, day: Optional[date] = None
    ) -> Optional[Quote]:
        day = day or self.day
        if day is None:
            return None
        sym = contract.underlying.upper()
        und = self.price(sym, day)
        dte = (contract.expiry - day).days
        if und is None or dte < 0:
            return None
        atm = self.atm_iv(sym, day, dte)
        vix = self.history.vol_on("VIX", day)
        if atm is None or vix is None:
            return None
        t = max(dte, 0.5) / 365.0
        q = self.p.dividend_yield.get(sym, 0.0)
        forward = und * math.exp((self.p.rate - q) * t)
        iv = self.skewed_iv(contract.strike, forward, atm, t, vix)
        flag = "p" if contract.right is Right.PUT else "c"
        args = (flag, und, contract.strike, t, self.p.rate, iv, q)
        mid = float(black_scholes_merton(*args))
        width = max(self.p.spread_pct * mid, self.p.min_width)
        bid = max(0.0, round(mid - width / 2, 2))
        ask = round(max(mid + width / 2, bid + 0.01), 2)
        greeks = Greeks(
            float(bsm_delta(*args)),
            float(bsm_gamma(*args)),
            float(bsm_theta(*args)),
            float(bsm_vega(*args)),
            iv=iv,
        )
        ts = datetime.combine(day, time(20, 0), tzinfo=timezone.utc)
        return Quote(contract, bid, ask, ts, underlying_price=und, greeks=greeks)

    def quotes(self, contracts: Iterable[OptionContract], day: date) -> List[Quote]:
        out = []
        for c in contracts:
            q = self.quote(c, day)
            if q is not None:
                out.append(q)
        return out

    def iv_series(self, symbol: str, until: date, days: int = 400) -> List[Bar]:
        """30-day ATM IV per day (fractions): the IMPLIED_VOL history the indicators use."""
        out = []
        for d in self.history.days(symbol):
            if until - timedelta(days=days) <= d <= until:
                v = self.atm_iv(symbol, d, 30)
                if v is not None:
                    out.append(Bar(d, v, v, v, v))
        return out


# -- validation against recorded IB chains -------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    n: int
    mean_abs_error: float  # $ per share, on mids
    mean_rel_error: float  # vs the recorded mid
    mean_iv_error: float  # synthetic - recorded, vol points (negative = conservative)
    worst: Tuple[str, float]  # key, $ error

    def within(self, rel: float = 0.25) -> bool:
        return self.n > 0 and self.mean_rel_error <= rel


def compare_quotes(
    recorded: Sequence[Quote], synthetic: Dict[str, Quote], min_mid: float = 0.20
) -> Comparison:
    """How far synthetic mids are from recorded ones (options with mid >= min_mid)."""
    errs, rels, ivs = [], [], []
    worst = ("", 0.0)
    for r in recorded:
        s = synthetic.get(r.contract.key)
        if s is None or r.mid is None or s.mid is None or r.mid < min_mid:
            continue
        e = s.mid - r.mid
        errs.append(abs(e))
        rels.append(abs(e) / r.mid)
        if r.greeks and r.greeks.iv and s.greeks and s.greeks.iv:
            ivs.append((s.greeks.iv - r.greeks.iv) * 100)
        if abs(e) > abs(worst[1]):
            worst = (r.contract.key, e)
    n = len(errs)
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return Comparison(n, mean(errs), mean(rels), mean(ivs), worst)
