"""Watchdog and nightly backups (plan T39, spec §15).

Runs as its **own process** (the ``watchdog`` service in docker-compose), so
it still works when the engine has crashed or hangs. Every 60 s it reads the
engine's heartbeat file:

| Check | Alert |
|---|---|
| Heartbeat older than 3 min, from 30 min before the open to 30 min after the close | **critical** "Engine down" (repeats every 15 min while down) |
| ... at other times, older than 30 min | warning "Engine down" |
| Engine up but not connected to IB for 10 min in market hours | **critical** "IB disconnected" (often the weekly IB Key re-login) |
| Account values stale for 10 min in market hours | warning |
| Back to normal after any of these | info "Engine back" |

**Nightly backup** (once a day, from 02:00 ET): the journal is copied with
SQLite's online backup (safe while the engine writes), plus the config
folder, into ``data/backups/YYYY-MM-DD/``. The last 14 are kept. Copy
``data/backups`` off the host too (spec §15), e.g. with rclone or a synced
folder.

The watchdog writes its own small heartbeat (``data/trading/watchdog.json``)
for the Docker health check.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

from src.trading.domain import utcnow
from src.trading.scheduler import ET

log = logging.getLogger("trading.watchdog")

DOWN_MARKET = timedelta(minutes=3)
DOWN_OFF_HOURS = timedelta(minutes=30)
DISCONNECTED_FOR = timedelta(minutes=10)
STALE_FOR = timedelta(minutes=10)
WINDOW = timedelta(minutes=30)  # around the session
BACKUP_AT = time(2, 0)
KEEP_BACKUPS = 14


@dataclass
class WatchState:
    down: bool = False
    disconnected_since: Optional[datetime] = None
    stale_since: Optional[datetime] = None
    alerted: set = field(default_factory=set)
    last_backup: Optional[str] = None  # ET date


def _read_heartbeat(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text())
        data["_ts"] = datetime.fromisoformat(data["ts"])
        return data
    except (OSError, ValueError, KeyError):
        return None


class Watchdog:
    def __init__(
        self,
        heartbeat: Path,
        notifier,
        calendar,
        journal_path: Path,
        config_dir: Path,
        backup_dir: Path,
        state_file: Optional[Path] = None,
    ):
        self.heartbeat = Path(heartbeat)
        self.notifier = notifier
        self.calendar = calendar
        self.journal_path = Path(journal_path)
        self.config_dir = Path(config_dir)
        self.backup_dir = Path(backup_dir)
        self.state_file = Path(state_file) if state_file else None
        self.state = WatchState()

    # -- engine health ----------------------------------------------------------------

    def in_market_window(self, now: datetime) -> bool:
        session = self.calendar.session(now.astimezone(ET).date())
        if session is None:
            return False
        open_, close = session
        return open_ - WINDOW <= now <= close + WINDOW

    def _alert(self, level, key, title, message) -> None:
        self.notifier.notify(level, key, title, message)
        self.state.alerted.add(key)

    def _recovered(self, key: str, title: str, message: str) -> None:
        if key in self.state.alerted:
            self.state.alerted.discard(key)
            self.notifier.notify("info", f"{key}:ok", title, message)

    def check(self, now: Optional[datetime] = None) -> dict:
        now = now or utcnow()
        market = self.in_market_window(now)
        hb = _read_heartbeat(self.heartbeat)
        age = (now - hb["_ts"]) if hb else None
        limit = DOWN_MARKET if market else DOWN_OFF_HOURS
        down = hb is None or age > limit
        result = {
            "market": market,
            "age_s": age.total_seconds() if age else None,
            "down": down,
        }

        if down:
            when = (
                "never started"
                if hb is None
                else f"last heartbeat {age.total_seconds() / 60:.0f} min ago"
            )
            self._alert(
                "critical" if market else "warning",
                "watchdog:engine-down",
                "Engine down",
                f"The trading engine isn't running ({when}). Open positions are not "
                "being watched: margin, rolls and guards are stopped. Check "
                "`docker compose ps` / `docker compose logs trading-engine`.",
            )
            self.state.down = True
            self.state.disconnected_since = self.state.stale_since = None
            return result
        if self.state.down:
            self.state.down = False
            self._recovered(
                "watchdog:engine-down",
                "Engine back",
                "The trading engine is running again.",
            )

        connected = bool(hb.get("connected"))
        if connected:
            self.state.disconnected_since = None
            self._recovered(
                "watchdog:ib", "IB connected", "The engine is connected to IB again."
            )
        else:
            self.state.disconnected_since = self.state.disconnected_since or now
            if market and now - self.state.disconnected_since >= DISCONNECTED_FOR:
                err = hb.get("last_error") or hb.get("connect_error") or ""
                self._alert(
                    "critical",
                    "watchdog:ib",
                    "IB disconnected",
                    "The engine is running but has no IB connection for "
                    f"{(now - self.state.disconnected_since).total_seconds() / 60:.0f} min"
                    + (f" ({err})" if err else "")
                    + ". Usually the weekly IB Key re-login: approve it on your phone, "
                    "or check the Gateway over VNC.",
                )
        result["connected"] = connected

        stale = bool(hb.get("account_stale"))
        if stale and connected:
            self.state.stale_since = self.state.stale_since or now
            if market and now - self.state.stale_since >= STALE_FOR:
                self._alert(
                    "warning",
                    "watchdog:stale",
                    "Account data stale",
                    "IB hasn't sent account values for 10 min. The engine treats margin as "
                    "at least Warning (no new trades) until they arrive.",
                )
        else:
            self.state.stale_since = None
            self._recovered(
                "watchdog:stale",
                "Account data fresh",
                "IB account values are arriving again.",
            )
        return result

    # -- backups ----------------------------------------------------------------------

    def backup_due(self, now: datetime) -> bool:
        et = now.astimezone(ET)
        return (
            et.time() >= BACKUP_AT and self.state.last_backup != et.date().isoformat()
        )

    def backup(self, now: Optional[datetime] = None) -> Optional[Path]:
        now = now or utcnow()
        day = now.astimezone(ET).date().isoformat()
        target = self.backup_dir / day
        target.mkdir(parents=True, exist_ok=True)
        if self.journal_path.exists():
            src = sqlite3.connect(str(self.journal_path))
            dst = sqlite3.connect(str(target / self.journal_path.name))
            try:
                src.backup(dst)  # consistent copy while the engine writes
            finally:
                dst.close()
                src.close()
        if self.config_dir.exists():
            shutil.copytree(self.config_dir, target / "config", dirs_exist_ok=True)
        self.state.last_backup = day
        self._prune()
        log.info("backup written to %s", target)
        return target

    def _prune(self) -> None:
        days = sorted(p for p in self.backup_dir.iterdir() if p.is_dir())
        for old in days[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)

    # -- loop ---------------------------------------------------------------------------

    def tick(self, now: Optional[datetime] = None) -> dict:
        now = now or utcnow()
        result = self.check(now)
        if self.backup_due(now):
            try:
                result["backup"] = str(self.backup(now))
            except (OSError, sqlite3.Error) as exc:
                log.error("backup failed: %s", exc)
                self._alert("warning", "watchdog:backup", "Backup failed", str(exc))
        if self.state_file is not None:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(
                json.dumps({"ts": now.isoformat(), **{k: v for k, v in result.items()}})
            )
        return result


def main() -> None:  # pragma: no cover - process entry point
    # pylint: disable=import-outside-toplevel
    from src.trading.calendars import MarketCalendar
    from src.trading.config import load_trading_config
    from src.trading.notifier import Notifier

    parser = argparse.ArgumentParser(description="Trading engine watchdog and backups")
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_trading_config()
    dog = Watchdog(
        heartbeat=Path(
            os.environ.get("TRADING_HEARTBEAT", "data/trading/heartbeat.json")
        ),
        notifier=Notifier(config.alerts),
        calendar=MarketCalendar(),
        journal_path=Path(os.environ.get("TRADING_JOURNAL", "data/trading/journal.db")),
        config_dir=Path("data/config"),
        backup_dir=Path(os.environ.get("TRADING_BACKUP_DIR", "data/backups")),
        state_file=Path("data/trading/watchdog.json"),
    )
    while True:
        try:
            dog.tick()
        except Exception:  # pylint: disable=broad-except
            log.exception("watchdog tick failed")
        if args.once:
            break
        _time.sleep(args.interval)


if __name__ == "__main__":  # pragma: no cover
    main()
