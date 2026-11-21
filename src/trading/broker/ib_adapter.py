"""Interactive Brokers adapter (plan T06+, spec §4.3).

This is the only module that imports ``ib_async``. Implemented so far:

* T06: connect to IB Gateway / TWS, verify the account, reconnect with backoff.
* T07: account values (net liquidation, excess liquidity, margin, Cushion).

Remaining data and order methods arrive in T08–T09, T18 and T22.

**Account value freshness:** IB pushes account values roughly every
3 minutes, not in real time. Snapshots carry the time of IB's last update,
and :meth:`IBAdapter.account_age_seconds` lets callers judge staleness
against :data:`ACCOUNT_STALE_AFTER_S`.

Safety rules enforced here, independent of the rest of the engine:

* The connection is **read-only by default**. ``ib_async`` is told to
  connect read-only too, so even a bug elsewhere can't send an order.
* A **live** account (live port, or an account ID not starting with ``DU``)
  can only be opened read-only until mode gating (T28) exists.
* The connection is refused if the configured account isn't one of the
  accounts IB reports for this login.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, List, Optional

from src.trading.broker.base import (
    EVENT_ACCOUNT,
    EVENT_CONNECTION,
    BrokerAdapter,
    BrokerDisconnected,
    BrokerError,
)
from src.trading.domain import AccountSnapshot, utcnow

log = logging.getLogger(__name__)

PAPER_PORTS = {4002, 7497}  # IB Gateway paper, TWS paper
LIVE_PORTS = {4001, 7496}  # IB Gateway live, TWS live

# Account tag -> AccountSnapshot field. These are IB's "current" values.
ACCOUNT_TAGS = {
    "NetLiquidation": "net_liquidation",
    "ExcessLiquidity": "excess_liquidity",
    "MaintMarginReq": "maint_margin",
    "InitMarginReq": "init_margin",
    "AvailableFunds": "available_funds",
    "TotalCashValue": "cash",
}
# IB refreshes account values about every 180 s; allow some slack.
ACCOUNT_STALE_AFTER_S = 240.0


class AccountDataUnavailable(BrokerError):
    """IB hasn't sent (all) account values yet."""


class AccountMismatch(BrokerError):
    """The login doesn't manage the configured account."""


class LiveTradingNotAllowed(BrokerError):
    """A live account was opened without read-only before mode gating exists."""


@dataclass(frozen=True)
class IBConnectionSettings:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    timeout_s: float = 10.0
    reconnect_min_s: float = 1.0
    reconnect_max_s: float = 60.0

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "IBConnectionSettings":
        env = os.environ if env is None else env
        return cls(
            host=env.get("IB_HOST", cls.host),
            port=int(env.get("IB_PORT", cls.port)),
            client_id=int(env.get("IB_CLIENT_ID", cls.client_id)),
        )

    @property
    def is_paper_port(self) -> bool:
        return self.port in PAPER_PORTS


def is_paper_account(account_id: str) -> bool:
    """IB paper accounts start with ``DU`` (paper sub-accounts: ``DF``)."""
    return account_id.upper().startswith(("DU", "DF"))


@dataclass
class ConnectionHealth:
    connected: bool = False
    last_connected_at: Optional[datetime] = None
    last_disconnected_at: Optional[datetime] = None
    reconnect_attempts: int = 0
    last_error: str = ""
    managed_accounts: List[str] = field(default_factory=list)


def _default_ib_factory():
    from ib_async import IB  # pylint: disable=import-outside-toplevel

    return IB()


class IBAdapter(BrokerAdapter):
    def __init__(
        self,
        account_id: str,
        settings: IBConnectionSettings = IBConnectionSettings(),
        read_only: bool = True,
        ib_factory: Callable[[], object] = _default_ib_factory,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
    ):
        if not account_id:
            raise ValueError("account_id is required (set TRADING_ACCOUNT_ID)")
        super().__init__(account_id, read_only)
        live = not is_paper_account(account_id) or not settings.is_paper_port
        if live and not read_only:
            raise LiveTradingNotAllowed(
                "live accounts are read-only until mode gating (plan T28) is in place"
            )
        self.settings = settings
        self.is_live = live
        self.health = ConnectionHealth()
        self.ib = ib_factory()
        self._sleep = sleep
        self._clock = clock
        self._closing = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._account_updated_at: Optional[datetime] = None
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.accountValueEvent += self._on_account_value

    # -- connection -------------------------------------------------------------

    async def connect(self) -> None:
        self._closing = False
        await self._connect_once()

    async def _connect_once(self) -> None:
        s = self.settings
        try:
            await self.ib.connectAsync(
                s.host,
                s.port,
                clientId=s.client_id,
                timeout=s.timeout_s,
                readonly=self.read_only,
                account=self.account_id,
            )
        except Exception as exc:  # ib_async raises several exception types
            self.health.last_error = f"{type(exc).__name__}: {exc}"
            raise BrokerError(
                f"cannot connect to IB at {s.host}:{s.port} (client {s.client_id}): {exc}"
            ) from exc

        accounts = list(self.ib.managedAccounts())
        self.health.managed_accounts = accounts
        if self.account_id not in accounts:
            self._closing = True
            self.ib.disconnect()
            raise AccountMismatch(
                f"account {self.account_id} is not managed by this login "
                f"(IB reports: {', '.join(accounts) or 'none'})"
            )

        self.health.connected = True
        self.health.last_connected_at = self._clock()
        self.health.reconnect_attempts = 0
        self.health.last_error = ""
        log.info(
            "connected to IB %s:%s account=%s read_only=%s live=%s",
            s.host,
            s.port,
            self.account_id,
            self.read_only,
            self.is_live,
        )
        self._publish(EVENT_CONNECTION, True)

    async def disconnect(self) -> None:
        self._closing = True
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self.ib.isConnected():
            self.ib.disconnect()
        else:
            self._mark_disconnected()

    @property
    def is_connected(self) -> bool:
        return self.health.connected and self.ib.isConnected()

    def _mark_disconnected(self) -> None:
        was_connected = self.health.connected
        self.health.connected = False
        self.health.last_disconnected_at = self._clock()
        if was_connected:
            self._publish(EVENT_CONNECTION, False)

    def _on_disconnected(self) -> None:
        self._mark_disconnected()
        if self._closing:
            return
        log.warning("IB connection lost; reconnecting")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (e.g. interpreter shutdown); nothing to schedule
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = self.settings.reconnect_min_s
        while not self._closing and not self.is_connected:
            await self._sleep(delay)
            if self._closing:
                return
            self.health.reconnect_attempts += 1
            try:
                await self._connect_once()
            except AccountMismatch:
                log.error("account mismatch on reconnect; giving up")
                return
            except BrokerError as exc:
                log.warning(
                    "reconnect attempt %d failed: %s",
                    self.health.reconnect_attempts,
                    exc,
                )
                delay = min(delay * 2, self.settings.reconnect_max_s)

    async def wait_reconnected(self) -> None:
        """Wait for a pending reconnect (used by tests and the supervisor)."""
        if self._reconnect_task is not None:
            await self._reconnect_task

    # -- account (T07) -----------------------------------------------------------

    def _account_values(self) -> dict:
        """Latest numeric value per tag, preferring the BASE-currency row."""
        # Multi-currency accounts report each tag per currency plus "BASE".
        # Prefer BASE, then USD, then anything else.
        values: dict = {}
        ranks: dict = {}
        for av in self.ib.accountValues(self.account_id):
            if (
                av.account != self.account_id
                or av.tag not in ACCOUNT_TAGS
                or av.modelCode
            ):
                continue
            rank = {"BASE": 0, "USD": 1}.get(av.currency, 2)
            if rank >= ranks.get(av.tag, 3):
                continue
            try:
                values[av.tag] = float(av.value)
            except (TypeError, ValueError):
                continue
            ranks[av.tag] = rank
        return values

    def _build_snapshot(self) -> AccountSnapshot:
        values = self._account_values()
        missing = [tag for tag in ACCOUNT_TAGS if tag not in values]
        if missing:
            raise AccountDataUnavailable(
                f"IB has not sent account values yet: missing {', '.join(missing)}"
            )
        fields = {ACCOUNT_TAGS[tag]: values[tag] for tag in ACCOUNT_TAGS}
        return AccountSnapshot(
            account_id=self.account_id,
            ts=self._account_updated_at or self._clock(),
            **fields,
        )

    async def account_snapshot(self) -> AccountSnapshot:
        if not self.is_connected:
            raise BrokerDisconnected("not connected to IB")
        return self._build_snapshot()

    def account_age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since IB last updated an account value (None if never)."""
        if self._account_updated_at is None:
            return None
        return ((now or self._clock()) - self._account_updated_at).total_seconds()

    def account_is_stale(self, now: Optional[datetime] = None) -> bool:
        age = self.account_age_seconds(now)
        return age is None or age > ACCOUNT_STALE_AFTER_S

    def _on_account_value(self, value) -> None:
        if value.account != self.account_id or value.tag not in ACCOUNT_TAGS:
            return
        self._account_updated_at = self._clock()
        try:
            snapshot = self._build_snapshot()
        except AccountDataUnavailable:
            return  # first batch still arriving
        self._publish(EVENT_ACCOUNT, snapshot)

    # -- not yet implemented (later plan tasks) ---------------------------------

    async def positions(self):
        raise NotImplementedError("plan T08")

    async def quote(self, contract):
        raise NotImplementedError("plan T09")

    async def option_chain(self, underlying):
        raise NotImplementedError("plan T09")

    async def what_if(self, order):
        raise NotImplementedError("plan T18")

    async def place_order(self, order):
        self._check_can_trade()
        raise NotImplementedError("plan T22")

    async def cancel_order(self, order_ref):
        raise NotImplementedError("plan T22")

    async def open_orders(self):
        raise NotImplementedError("plan T22")

    async def executions(self, since=None):
        raise NotImplementedError("plan T22")
