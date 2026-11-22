"""Bot status for the web UI (plan T12).

The web UI never talks to IB for this page. It reads what the engine
writes: the heartbeat file (connection state, refreshed every loop) and the
journal (latest account and position snapshots). If the engine is down,
the page still loads and says so.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional

from src.trading.config import MarginFloorConfig
from src.trading.domain import AccountSnapshot, Position, utcnow
from src.trading.journal import Journal

ENGINE_DOWN_AFTER_S = 300  # heartbeat older than this = engine not running


class MarginState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    BREACH = "breach"
    EMERGENCY = "emergency"
    UNKNOWN = "unknown"


def classify_margin(
    snapshot: Optional[AccountSnapshot], floor: MarginFloorConfig
) -> MarginState:
    """Margin state per spec §6.5 from one snapshot (no hysteresis; see T16)."""
    if snapshot is None or snapshot.net_liquidation <= 0:
        return MarginState.UNKNOWN
    el, cushion = snapshot.excess_liquidity, snapshot.cushion
    if el < floor.emergency_level or cushion < floor.cushion_emergency:
        return MarginState.EMERGENCY
    if el < floor.excess_liquidity_min or cushion < floor.cushion_breach:
        return MarginState.BREACH
    if el < floor.entry_min_excess_liquidity or cushion < floor.cushion_min_new_trade:
        return MarginState.WARNING
    return MarginState.NORMAL


@dataclass
class BotStatus:
    engine_running: bool = False
    heartbeat_age_s: Optional[float] = None
    connected: bool = False
    read_only: Optional[bool] = None
    live: Optional[bool] = None
    account_stale: Optional[bool] = None
    last_error: str = ""
    account: Optional[AccountSnapshot] = None
    account_age_s: Optional[float] = None
    margin_state: MarginState = MarginState.UNKNOWN
    positions: List[Position] = field(default_factory=list)
    positions_at: Optional[datetime] = None
    reconcile: Optional[dict] = None  # latest reconciliation result (plain dict)

    def to_json(self) -> dict:
        acct = self.account
        return {
            "engine_running": self.engine_running,
            "heartbeat_age_s": self.heartbeat_age_s,
            "connected": self.connected,
            "read_only": self.read_only,
            "live": self.live,
            "account_stale": self.account_stale,
            "last_error": self.last_error,
            "margin_state": self.margin_state.value,
            "reconcile": self.reconcile,
            "account_age_s": self.account_age_s,
            "account": None
            if acct is None
            else {
                "account_id": acct.account_id,
                "net_liquidation": acct.net_liquidation,
                "excess_liquidity": acct.excess_liquidity,
                "cushion": acct.cushion,
                "maint_margin": acct.maint_margin,
                "init_margin": acct.init_margin,
                "available_funds": acct.available_funds,
                "cash": acct.cash,
            },
            "positions": [
                {
                    "key": p.contract.key,
                    "quantity": p.quantity,
                    "avg_cost": p.avg_cost,
                    "market_price": p.market_price,
                }
                for p in self.positions
            ],
        }


def write_heartbeat(path: Path | str, **state) -> None:
    """Engine side: record connection state for the UI (atomic write)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": utcnow().isoformat(), **state}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def load_bot_status(
    journal_path: Path | str,
    heartbeat_path: Path | str,
    floor: MarginFloorConfig,
    now: Optional[datetime] = None,
) -> BotStatus:
    now = now or utcnow()
    status = BotStatus()

    hb_path = Path(heartbeat_path)
    try:
        heartbeat = json.loads(hb_path.read_text())
        hb_ts = datetime.fromisoformat(heartbeat["ts"])
        status.heartbeat_age_s = (now - hb_ts).total_seconds()
        status.engine_running = status.heartbeat_age_s <= ENGINE_DOWN_AFTER_S
        if status.engine_running:
            status.connected = bool(heartbeat.get("connected"))
        status.read_only = heartbeat.get("read_only")
        status.live = heartbeat.get("live")
        status.account_stale = heartbeat.get("account_stale")
        status.last_error = heartbeat.get("last_error", "")
    except (OSError, ValueError, KeyError):
        pass  # never started, or unreadable: engine shown as not running

    if Path(journal_path).exists():
        journal = Journal(journal_path)
        try:
            acct = journal.latest("account_snapshot")
            if acct is not None:
                status.account = acct.data
                status.account_age_s = (now - acct.data.ts).total_seconds()
            rec = journal.latest("reconciliations")
            if rec is not None:
                status.reconcile = rec.data
            pos = journal.latest("positions_snapshot")
            if pos is not None:
                status.positions = list(pos.data)
                status.positions_at = pos.ts
        finally:
            journal.close()
    status.margin_state = classify_margin(status.account, floor)
    return status
