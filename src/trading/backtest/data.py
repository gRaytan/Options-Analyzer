"""Free history for the synthetic backtest (plan T36, spec §11.1).

| Series | Source | Notes |
|---|---|---|
| VIX, VIX9D, VIX3M, VXN | CBOE daily CSVs (free, no login) | VIX from 1990; VIX9D from 2011; VIX3M and VXN from Sept 2009 |
| SPY, QQQ, QQQM, SPX daily bars | IB ``reqHistoricalData`` on your machine, or yfinance, or a CSV you drop in | IB serves ~20 years of daily bars |

Everything is cached as CSV under ``data/backtest/`` (git-ignored), so a
backtest re-run needs no network. Bars CSVs are ``date,open,high,low,close,volume``
(ISO dates), the same format :func:`bars_to_csv` writes.

XSP is SPX / 10: its path comes from SPX bars divided by 10 when you have
them, else from SPY (close enough for a strategy test: SPY trades at about
SPX / 10 less dividends).
"""

from __future__ import annotations

import csv
import io
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.trading.domain import Bar

log = logging.getLogger("trading.backtest.data")

CBOE_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv"
VOL_INDICES = ("VIX", "VIX9D", "VIX3M", "VXN")


def default_dir() -> Path:
    return Path(os.environ.get("BACKTEST_DATA_DIR", "data/backtest"))


# -- parsing ---------------------------------------------------------------------


def _parse_day(text: str) -> date:
    text = text.strip()
    if "/" in text:  # CBOE: mm/dd/yyyy
        return datetime.strptime(text, "%m/%d/%Y").date()
    return date.fromisoformat(text[:10])


def parse_cboe_csv(text: str) -> Dict[date, float]:
    """CBOE index history (DATE,OPEN,HIGH,LOW,CLOSE) -> {day: close in points}."""
    out: Dict[date, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().upper(): v for k, v in row.items() if k}
        try:
            close = float(row["CLOSE"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            out[_parse_day(row["DATE"])] = close
    return out


def parse_bars_csv(text: str) -> List[Bar]:
    """``date,open,high,low,close[,volume]`` (any case; yfinance/IB exports work) -> bars."""
    bars = []
    for row in csv.DictReader(io.StringIO(text)):
        row = {k.strip().lower(): v for k, v in row.items() if k}
        try:
            close = float(row["close"])
            bars.append(
                Bar(
                    _parse_day(row["date"]),
                    float(row.get("open") or close),
                    float(row.get("high") or close),
                    float(row.get("low") or close),
                    close,
                    float(row.get("volume") or 0.0),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.day)
    return bars


def bars_to_csv(bars: List[Bar]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["date", "open", "high", "low", "close", "volume"])
    for b in bars:
        w.writerow([b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume])
    return buf.getvalue()


# -- fetching (with a CSV cache) -----------------------------------------------------


def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "options-analyzer"})
    with urllib.request.urlopen(
        req, timeout=30
    ) as resp:  # noqa: S310 (fixed https URL)
        return resp.read().decode("utf-8")


def fetch_cboe(
    name: str,
    cache_dir: Optional[Path] = None,
    refresh: bool = False,
    get: Callable[[str], str] = _http_get,
) -> Dict[date, float]:
    """One CBOE volatility index, from the cache or CBOE's website."""
    cache_dir = Path(cache_dir or default_dir())
    path = cache_dir / f"{name.upper()}.cboe.csv"
    if path.exists() and not refresh:
        return parse_cboe_csv(path.read_text())
    text = get(CBOE_URL.format(name=name.upper()))
    series = parse_cboe_csv(text)
    if series:  # never cache an empty or broken answer
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return series


def bars_path(symbol: str, cache_dir: Optional[Path] = None) -> Path:
    return Path(cache_dir or default_dir()) / f"{symbol.upper()}.csv"


def load_bars(symbol: str, cache_dir: Optional[Path] = None) -> List[Bar]:
    path = bars_path(symbol, cache_dir)
    return parse_bars_csv(path.read_text()) if path.exists() else []


def save_bars(symbol: str, bars: List[Bar], cache_dir: Optional[Path] = None) -> Path:
    path = bars_path(symbol, cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bars_to_csv(bars))
    return path


def fetch_yfinance(symbol: str, start: date) -> List[Bar]:
    """Daily bars from Yahoo (best effort: Yahoo rate-limits and blocks some networks)."""
    import yfinance as yf  # pylint: disable=import-outside-toplevel

    ticker = {"SPX": "^GSPC", "NDX": "^NDX"}.get(symbol.upper(), symbol.upper())
    frame = yf.download(
        ticker, start=start.isoformat(), progress=False, auto_adjust=False
    )
    bars = []
    for ts, row in frame.iterrows():

        def val(col):
            v = row[col]
            return float(v.iloc[0] if hasattr(v, "iloc") else v)

        bars.append(Bar(ts.date(), val("Open"), val("High"), val("Low"), val("Close")))
    return bars


async def fetch_ib(broker, symbol: str, years: int = 18) -> List[Bar]:
    """Daily TRADES bars from IB (``broker``: a connected adapter)."""
    from src.trading.domain import (  # pylint: disable=import-outside-toplevel
        HistoryKind,
    )

    return await broker.daily_history(symbol, HistoryKind.PRICE, years * 365)


# -- the assembled history ----------------------------------------------------------------


@dataclass
class MarketHistory:
    """Daily underlying bars plus the CBOE volatility indices, for one backtest."""

    bars: Dict[str, List[Bar]] = field(default_factory=dict)
    vol: Dict[str, Dict[date, float]] = field(default_factory=dict)  # points

    def closes(self, symbol: str) -> Dict[date, float]:
        return {b.day: b.close for b in self.bars.get(symbol.upper(), [])}

    def days(self, symbol: str) -> List[date]:
        return [b.day for b in self.bars.get(symbol.upper(), [])]

    def vol_on(self, name: str, day: date) -> Optional[float]:
        """Index close on ``day`` as an annualized fraction (VIX 20 -> 0.20)."""
        v = self.vol.get(name.upper(), {}).get(day)
        return v / 100.0 if v else None

    def with_xsp(self) -> "MarketHistory":
        """Add XSP bars: SPX / 10 when SPX is loaded, else SPY."""
        if "XSP" in self.bars:
            return self
        if "SPX" in self.bars:
            src, f = self.bars["SPX"], 0.1
        elif "SPY" in self.bars:
            src, f = self.bars["SPY"], 1.0
        else:
            return self
        self.bars["XSP"] = [
            Bar(b.day, b.open * f, b.high * f, b.low * f, b.close * f, b.volume)
            for b in src
        ]
        return self


def load_history(
    symbols: List[str],
    cache_dir: Optional[Path] = None,
    get: Callable[[str], str] = _http_get,
    refresh_vol: bool = False,
) -> MarketHistory:
    """Bars from the cache (fetch them first with scripts/backtest_data.py) + CBOE indices."""
    hist = MarketHistory()
    for s in symbols:
        bars = load_bars(s, cache_dir)
        if bars:
            hist.bars[s.upper()] = bars
        else:
            log.warning("no cached bars for %s (run scripts/backtest_data.py)", s)
    for name in VOL_INDICES:
        try:
            hist.vol[name] = fetch_cboe(name, cache_dir, refresh_vol, get)
        except OSError as exc:
            log.warning("CBOE %s unavailable: %s", name, exc)
    return hist.with_xsp()


# -- recorded IB chains (for calibrating the synthetic skew) --------------------------

RECORDED_FIELDS = (
    "day",
    "underlying",
    "trading_class",
    "expiry",
    "strike",
    "right",
    "bid",
    "ask",
    "iv",
    "delta",
    "underlying_price",
)


def recorded_to_csv(quotes, day: date) -> str:
    """Live quotes (with Greeks) -> CSV, one row per option."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(RECORDED_FIELDS)
    for q in quotes:
        c = q.contract
        g = q.greeks
        w.writerow(
            [
                day.isoformat(),
                c.underlying,
                c.trading_class or c.underlying,
                c.expiry.isoformat(),
                c.strike,
                c.right.value,
                q.bid,
                q.ask,
                g.iv if g else "",
                g.delta if g else "",
                q.underlying_price or "",
            ]
        )
    return buf.getvalue()


def recorded_from_csv(text: str):
    """CSV from :func:`recorded_to_csv` -> (day, underlying price, [Quote])."""
    # pylint: disable=import-outside-toplevel
    from datetime import time, timezone

    from src.trading.domain import ExerciseStyle, Greeks, OptionContract, Quote, Right

    quotes, day, und = [], None, None
    for row in csv.DictReader(io.StringIO(text)):
        day = date.fromisoformat(row["day"])
        und = float(row["underlying_price"]) if row["underlying_price"] else und
        sym = row["underlying"].upper()
        index = sym in ("XSP", "SPX")
        c = OptionContract(
            sym,
            date.fromisoformat(row["expiry"]),
            float(row["strike"]),
            Right(row["right"]),
            100,
            ExerciseStyle.EUROPEAN if index else ExerciseStyle.AMERICAN,
            index,
            row["trading_class"] or sym,
        )
        greeks = (
            Greeks(float(row["delta"] or 0.0), iv=float(row["iv"]))
            if row["iv"]
            else None
        )
        ts = datetime.combine(day, time(20, 0), tzinfo=timezone.utc)
        quotes.append(
            Quote(
                c,
                float(row["bid"]) if row["bid"] else None,
                float(row["ask"]) if row["ask"] else None,
                ts,
                underlying_price=und,
                greeks=greeks,
            )
        )
    return day, und, quotes
