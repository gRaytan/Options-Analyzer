"""Margin floor monitor (plan T16, spec §6.5, R1, R9). Alerts only, no closing yet.

Every check classifies the account into normal / warning / breach /
emergency from Excess Liquidity (EL) and Cushion, with three refinements:

* **Conservative estimate between IB refreshes.** IB pushes account values
  about every 3 minutes, but its daily P&L streams about every second. Since
  the last IB refresh, P&L **losses** are subtracted from EL and net
  liquidation, and **gains are ignored**. The state can worsen between
  refreshes, but only IB's own numbers can improve it.
* **Hysteresis.** Once in breach (or emergency), the state only returns to
  warning when EL >= ``restore_target`` ($110k) **and** Cushion >=
  ``cushion_restore`` (35%), so one small move doesn't flap the state.
* **Stale data.** Account values older than 240 s count as at least warning.

Automatic closing (margin restoration, T26) acts on this monitor's breach state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional

from src.trading.config import MarginFloorConfig
from src.trading.domain import AccountSnapshot, utcnow
from src.trading.status import ACCOUNT_STALE_AFTER_S, MarginState, classify_margin

SEVERITY = {
    MarginState.UNKNOWN: 1,  # treated like warning: no data, no new risk
    MarginState.NORMAL: 0,
    MarginState.WARNING: 1,
    MarginState.BREACH: 2,
    MarginState.EMERGENCY: 3,
}


@dataclass(frozen=True)
class MarginReading:
    state: MarginState
    excess_liquidity: Optional[float]  # estimated (<= IB value)
    cushion: Optional[float]
    ib_excess_liquidity: Optional[float]
    pnl_since_refresh: float
    stale: bool
    ts: datetime
    reason: str = ""

    @property
    def allows_opening(self) -> bool:
        return self.state is MarginState.NORMAL

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "excess_liquidity": self.excess_liquidity,
            "cushion": self.cushion,
            "ib_excess_liquidity": self.ib_excess_liquidity,
            "pnl_since_refresh": self.pnl_since_refresh,
            "stale": self.stale,
            "ts": self.ts.isoformat(),
            "reason": self.reason,
        }


Listener = Callable[[MarginReading, Optional[MarginReading]], None]


class MarginMonitor:
    def __init__(
        self, floor: MarginFloorConfig, clock: Callable[[], datetime] = utcnow
    ):
        self.floor = floor
        self._clock = clock
        self._snapshot_ts: Optional[datetime] = None
        self._pnl_at_refresh: Optional[float] = None
        self.last: Optional[MarginReading] = None
        self._listeners: List[Listener] = []
        # Breach latch: set on breach/emergency, cleared only by a restore.
        # It survives "no data" readings and, via restore_state(), restarts.
        self.latched = False

    def restore_state(self, last_state: Optional[str]) -> None:
        """Seed the breach latch from the journal after a restart."""
        if last_state in (MarginState.BREACH.value, MarginState.EMERGENCY.value):
            self.latched = True

    def on_change(self, listener: Listener) -> None:
        """Called as listener(new, previous) whenever the state changes."""
        self._listeners.append(listener)

    def update(
        self,
        snapshot: Optional[AccountSnapshot],
        daily_pnl: Optional[float] = None,
        pnl_at_snapshot: Optional[float] = None,
    ) -> MarginReading:
        """``pnl_at_snapshot``: daily P&L when IB sent the snapshot, if known.

        Without it, the baseline is the P&L when this monitor first sees the
        snapshot, which can miss up to one check interval of losses.
        """
        now = self._clock()
        if snapshot is None:
            state = MarginState.BREACH if self.latched else MarginState.UNKNOWN
            reason = "no account data" + (
                "; breach not yet restored" if self.latched else ""
            )
            reading = MarginReading(state, None, None, None, 0.0, True, now, reason)
            return self._emit(reading)

        if snapshot.ts != self._snapshot_ts:  # a fresh IB refresh arrived
            self._snapshot_ts = snapshot.ts
            self._pnl_at_refresh = (
                pnl_at_snapshot if pnl_at_snapshot is not None else daily_pnl
            )
        loss = 0.0
        if daily_pnl is not None and self._pnl_at_refresh is not None:
            loss = min(0.0, daily_pnl - self._pnl_at_refresh)  # gains ignored
        elif daily_pnl is not None and self._pnl_at_refresh is None:
            self._pnl_at_refresh = daily_pnl  # P&L stream started after the refresh

        est = AccountSnapshot(
            snapshot.account_id,
            snapshot.ts,
            snapshot.net_liquidation + loss,
            snapshot.excess_liquidity + loss,
            snapshot.maint_margin,
            snapshot.init_margin,
            snapshot.available_funds + loss,
            snapshot.cash,
        )
        raw = classify_margin(est, self.floor)
        age = (now - snapshot.ts).total_seconds()
        stale = age > ACCOUNT_STALE_AFTER_S
        reasons = []
        if loss < 0:
            reasons.append(f"includes ${-loss:,.0f} P&L loss since IB's last refresh")
        state = raw
        if stale and SEVERITY[state] < SEVERITY[MarginState.WARNING]:
            state = MarginState.WARNING
            reasons.append(f"account data {age:.0f}s old")
        state = self._apply_hysteresis(state, est)
        reading = MarginReading(
            state,
            est.excess_liquidity,
            est.cushion,
            snapshot.excess_liquidity,
            loss,
            stale,
            now,
            "; ".join(reasons),
        )
        return self._emit(reading)

    def _apply_hysteresis(
        self, state: MarginState, est: AccountSnapshot
    ) -> MarginState:
        if SEVERITY[state] >= SEVERITY[MarginState.BREACH]:
            self.latched = True
            return state  # in (or back to) breach/emergency: take it as is
        if not self.latched:
            return state
        restored = (
            est.excess_liquidity >= self.floor.restore_target
            and est.cushion >= self.floor.cushion_restore
        )
        if restored:
            self.latched = False
            return state
        return MarginState.BREACH

    def _emit(self, reading: MarginReading) -> MarginReading:
        previous = self.last
        self.last = reading
        if previous is None or previous.state is not reading.state:
            for listener in list(self._listeners):
                listener(reading, previous)
        return reading
