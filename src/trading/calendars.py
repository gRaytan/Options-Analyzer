"""Market hours and corporate events (plan T14, spec §5.1, §5.11, §8).

* :class:`MarketCalendar`: NYSE sessions, holidays, early closes, DST, via
  ``exchange_calendars``.
* :class:`EventCalendar`: earnings and ex-dividend dates per symbol.
  Sources, in priority order:
    1. Manual overrides in ``data/config/events.json`` (you can always correct
       a date by hand).
    2. A provider (default: Yahoo Finance via ``yfinance``, free but
       unofficial), cached once per day.

  **Unknown is not "none".** If a stock's next earnings date can't be found,
  ``earnings_before`` returns ``None``, and strategies must treat that as
  "might have earnings" and skip single-stock entries (spec §5.1). ETFs and
  indexes have no earnings.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

# Funds and indexes: no earnings. ETFs still have ex-dividend dates.
NO_EARNINGS = {
    "SPY", "QQQ", "QQQM", "IWM", "DIA", "VOO", "VTI", "IVV",
    "XSP", "SPX", "SPXW", "NDX", "XND", "RUT", "MRUT", "VIX", "DJX",
}  # fmt: skip
INDEXES = {"XSP", "SPX", "SPXW", "NDX", "XND", "RUT", "MRUT", "VIX", "DJX"}

DEFAULT_OVERRIDES = Path("data/config/events.json")
DEFAULT_CACHE = Path("data/trading/cache/events.json")
UNKNOWN_RETRY = timedelta(hours=1)


class MarketCalendar:
    """NYSE trading calendar. All datetimes are timezone-aware UTC."""

    def __init__(self, exchange: str = "XNYS"):
        import exchange_calendars as xcals  # pylint: disable=import-outside-toplevel

        self._cal = xcals.get_calendar(exchange)

    def is_trading_day(self, day: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(day)))

    def is_open(self, ts: datetime) -> bool:
        return bool(self._cal.is_open_on_minute(pd.Timestamp(ts).tz_convert("UTC")))

    def session(self, day: date) -> Optional[Tuple[datetime, datetime]]:
        """(open, close) in UTC for a trading day, else None."""
        if not self.is_trading_day(day):
            return None
        session = pd.Timestamp(day)
        return (
            self._cal.session_open(session).to_pydatetime(),
            self._cal.session_close(session).to_pydatetime(),
        )

    def is_early_close(self, day: date) -> bool:
        times = self.session(day)
        if times is None:
            return False
        open_, close = times
        return (close - open_) < timedelta(hours=6, minutes=30)

    def next_trading_day(self, day: date) -> date:
        """The first trading day strictly after ``day``."""
        nxt = self._cal.date_to_session(
            pd.Timestamp(day + timedelta(days=1)), direction="next"
        )
        return nxt.date()

    def previous_trading_day(self, day: date) -> date:
        prev = self._cal.date_to_session(
            pd.Timestamp(day - timedelta(days=1)), direction="previous"
        )
        return prev.date()

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days in (start, end]: e.g. trading days left until expiry."""
        if end <= start:
            return 0
        sessions = self._cal.sessions_in_range(
            pd.Timestamp(start + timedelta(days=1)), pd.Timestamp(end)
        )
        return len(sessions)


@dataclass(frozen=True)
class Dividend:
    ex_date: date
    amount: float


@dataclass(frozen=True)
class SymbolEvents:
    """What's known about a symbol's next events (``None`` = unknown)."""

    earnings: Optional[date] = None
    dividend: Optional[Dividend] = None
    earnings_known: bool = False  # True if the source answered (even "no earnings")


Provider = Callable[[str], SymbolEvents]


def yfinance_provider(symbol: str) -> SymbolEvents:
    """Next earnings and ex-dividend date from Yahoo Finance (best effort)."""
    import yfinance as yf  # pylint: disable=import-outside-toplevel

    ticker = yf.Ticker(symbol)
    cal = ticker.calendar or {}
    if not isinstance(cal, dict):  # older yfinance returned a DataFrame
        cal = {}
    earnings = None
    raw = cal.get("Earnings Date")
    if isinstance(raw, (list, tuple)) and raw:
        earnings = min(raw)
    elif isinstance(raw, date):
        earnings = raw
    dividend = None
    ex_date = cal.get("Ex-Dividend Date")
    if isinstance(ex_date, date):
        divs = ticker.dividends
        amount = float(divs.iloc[-1]) if divs is not None and len(divs) else 0.0
        dividend = Dividend(ex_date, amount)
    return SymbolEvents(earnings, dividend, earnings_known=earnings is not None)


def _to_dict(ev: SymbolEvents) -> dict:
    return {
        "earnings": ev.earnings.isoformat() if ev.earnings else None,
        "earnings_known": ev.earnings_known,
        "dividend": (
            {"ex_date": ev.dividend.ex_date.isoformat(), "amount": ev.dividend.amount}
            if ev.dividend
            else None
        ),
    }


def _from_dict(d: dict) -> SymbolEvents:
    div = d.get("dividend")
    return SymbolEvents(
        earnings=date.fromisoformat(d["earnings"]) if d.get("earnings") else None,
        dividend=Dividend(date.fromisoformat(div["ex_date"]), float(div["amount"]))
        if div
        else None,
        earnings_known=bool(d.get("earnings_known", d.get("earnings") is not None)),
    )


class EventCalendar:
    def __init__(
        self,
        provider: Optional[Provider] = yfinance_provider,
        overrides_path: Path | str = DEFAULT_OVERRIDES,
        cache_path: Path | str = DEFAULT_CACHE,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.provider = provider
        self.overrides_path = Path(overrides_path)
        self.cache_path = Path(cache_path)
        self._clock = clock
        self._cache: Dict[str, dict] = self._load_cache()

    def _load_cache(self) -> Dict[str, dict]:
        try:
            return json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache))
        tmp.replace(self.cache_path)

    def _overrides(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.overrides_path.read_text())
        except (OSError, ValueError):
            return {}
        return {k.upper(): v for k, v in data.items() if not k.startswith("_")}

    def events(self, symbol: str) -> SymbolEvents:
        symbol = symbol.upper()
        override = self._overrides().get(symbol)
        if override is not None:
            return _from_dict({"earnings_known": True, **override})
        now = self._clock()
        today = now.date().isoformat()
        cached = self._cache.get(symbol)
        if cached and cached.get("fetched_on") == today:
            ev = _from_dict(cached["events"])
            # Known answers last the day; "unknown" (often a rate-limited or
            # failed lookup) is retried after an hour.
            fetched_at = datetime.fromisoformat(
                cached.get("fetched_at", now.isoformat())
            )
            if ev.earnings_known or now - fetched_at < UNKNOWN_RETRY:
                return ev
        if self.provider is None:
            return SymbolEvents(earnings_known=symbol in NO_EARNINGS)
        try:
            ev = self.provider(symbol)
        except Exception as exc:  # network / parsing: treat as unknown, don't cache
            log.warning("event lookup failed for %s: %s", symbol, exc)
            return SymbolEvents(earnings_known=symbol in NO_EARNINGS)
        if symbol in NO_EARNINGS:
            ev = SymbolEvents(None, ev.dividend, earnings_known=True)
        self._cache[symbol] = {
            "fetched_on": today,
            "fetched_at": now.isoformat(),
            "events": _to_dict(ev),
        }
        self._save_cache()
        return ev

    def earnings_before(self, symbol: str, expiry: date, today: date) -> Optional[bool]:
        """True if earnings fall between today and expiry (inclusive).

        ``None`` means unknown. Callers must not treat that as False.
        """
        if symbol.upper() in NO_EARNINGS:
            return False
        ev = self.events(symbol)
        if ev.earnings is None:
            return None if not ev.earnings_known else False
        return today <= ev.earnings <= expiry

    def ex_dividend_before(
        self, symbol: str, expiry: date, today: date
    ) -> Optional[Dividend]:
        """The dividend whose ex-date falls between today and expiry, if any."""
        if symbol.upper() in INDEXES:
            return None  # cash-settled indexes: no dividend assignment risk
        div = self.events(symbol).dividend
        if div is not None and today <= div.ex_date <= expiry:
            return div
        return None
