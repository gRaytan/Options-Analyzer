"""Trading engine entry point: ``python -m src.trading.run`` (plan T11).

For now the engine is a **read-only monitor**: it connects to IB, keeps the
connection alive (reconnecting after the nightly restart), and journals an
account snapshot every interval. Trading logic is added by later tasks
(the scheduler in T38 replaces this loop). It never places orders.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from src.trading.broker.base import BrokerError
from src.trading.broker.ib_adapter import IBAdapter, IBConnectionSettings
from src.trading.config import load_trading_config
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal
from src.trading.status import write_heartbeat

log = logging.getLogger("trading.run")

HEARTBEAT_FILE = "data/trading/heartbeat.json"


async def monitor(
    interval_s: float, once: bool = False, adapter=None, journal=None
) -> int:
    """Run the read-only monitor loop. ``adapter``/``journal`` are injectable for tests."""
    if adapter is None:
        config = load_trading_config()
        if not config.account_id:
            log.error("TRADING_ACCOUNT_ID is not set")
            return 2
        adapter = IBAdapter(
            config.account_id, IBConnectionSettings.from_env(), read_only=True
        )
    if journal is None:
        journal = Journal(os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH)))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    delay = 5.0
    while not stop.is_set():
        try:
            await adapter.connect()
            break
        except BrokerError as exc:
            log.warning("connect failed (%s); retrying in %.0fs", exc, delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    while not stop.is_set():
        try:
            snap = await adapter.account_snapshot()
            journal.append("account_snapshot", snap, key=snap.account_id)
            log.info(
                "NLV $%.0f  EL $%.0f  cushion %.1f%%%s",
                snap.net_liquidation,
                snap.excess_liquidity,
                snap.cushion * 100,
                "  (stale)" if adapter.account_is_stale() else "",
            )
        except BrokerError as exc:
            log.warning("no account snapshot: %s", exc)
        try:
            positions = await adapter.positions()
            journal.append(
                "positions_snapshot", tuple(positions), key=adapter.account_id
            )
        except BrokerError as exc:
            log.warning("no positions: %s", exc)
        _heartbeat(adapter)
        if once:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

    await adapter.disconnect()
    journal.close()
    return 0


def _heartbeat(adapter) -> None:
    health = getattr(adapter, "health", None)
    write_heartbeat(
        HEARTBEAT_FILE,
        connected=adapter.is_connected,
        read_only=adapter.read_only,
        live=getattr(adapter, "is_live", False),
        account_stale=adapter.account_is_stale(),
        last_error=getattr(health, "last_error", ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Options Analyzer trading engine")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between snapshots"
    )
    parser.add_argument(
        "--once", action="store_true", help="take one snapshot and exit"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(monitor(args.interval, args.once)))


if __name__ == "__main__":
    main()
