"""Daily history with an on-disk cache (plan T10).

Indicators (T29) need a year of daily prices and implied volatility per
underlying. Fetching that from IB on every scan would waste pacing budget,
so series are cached as JSON under ``data/trading/cache/history`` and
refreshed at most once per day (after the close the day's bar is final).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Callable, List

from src.trading.broker.base import BrokerAdapter
from src.trading.domain import Bar, HistoryKind, from_dict, to_dict, utcnow

DEFAULT_CACHE_DIR = Path("data/trading/cache/history")


class HistoryStore:
    def __init__(
        self,
        broker: BrokerAdapter,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.broker = broker
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def _path(self, symbol: str, kind: HistoryKind, days: int) -> Path:
        return self.cache_dir / f"{symbol.upper()}_{kind.name.lower()}_{days}d.json"

    def _read(self, path: Path):
        try:
            data = json.loads(path.read_text())
            return date.fromisoformat(data["fetched_on"]), from_dict(data["bars"])
        except (OSError, ValueError, KeyError):
            return None, None  # missing or corrupt cache: refetch

    async def daily(self, symbol: str, kind: HistoryKind, days: int = 365) -> List[Bar]:
        """Bars for the last ``days`` calendar days, served from cache when fresh."""
        path = self._path(symbol, kind, days)
        today = self._clock().date()
        fetched_on, bars = self._read(path)
        if fetched_on == today and bars is not None:
            return list(bars)
        bars = await self.broker.daily_history(symbol, kind, days)
        payload = {"fetched_on": today.isoformat(), "bars": to_dict(tuple(bars))}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)  # atomic: a crash never leaves a half-written cache
        return list(bars)

    async def closes(self, symbol: str, days: int = 365) -> List[float]:
        return [b.close for b in await self.daily(symbol, HistoryKind.PRICE, days)]

    async def implied_vols(self, symbol: str, days: int = 365) -> List[float]:
        return [
            b.close for b in await self.daily(symbol, HistoryKind.IMPLIED_VOL, days)
        ]
