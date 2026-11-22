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
from src.trading.domain import HaltScope, utcnow
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal
from src.trading.kill_switch import DEFAULT_HALT_FILE, HaltManager
from src.trading.margin_monitor import MarginMonitor, MarginReading
from src.trading.reconcile import expected_from_journal, reconcile, result_to_dict
from src.trading.risk.budget import BudgetTracker
from src.trading.status import write_heartbeat

log = logging.getLogger("trading.run")

HEARTBEAT_FILE = "data/trading/heartbeat.json"


async def monitor(
    interval_s: float, once: bool = False, adapter=None, journal=None, halt_file=None
) -> int:
    """Run the read-only monitor loop. ``adapter``/``journal`` are injectable for tests."""
    config = load_trading_config()
    if adapter is None:
        if not config.account_id:
            log.error("TRADING_ACCOUNT_ID is not set")
            return 2
        adapter = IBAdapter(
            config.account_id, IBConnectionSettings.from_env(), read_only=True
        )
    if journal is None:
        journal = Journal(os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH)))
    halts = HaltManager(
        journal,
        halt_file or os.environ.get("TRADING_HALT_FILE", str(DEFAULT_HALT_FILE)),
    )
    margin = MarginMonitor(config.margin_floor)
    margin.on_change(
        lambda new, old: _on_margin_change(journal, adapter.account_id, new, old)
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    budget = BudgetTracker(config, journal)

    # Restore the breach latch, so a restart during a breach stays in breach.
    last_margin = journal.latest("margin_events")
    if last_margin is not None:
        margin.restore_state(last_margin.data.get("state"))

    last_reconcile = None
    reconcile_state = {"reconcile_ok": None, "mismatches": 0, "unmanaged": 0}
    delay = 5.0
    while not stop.is_set():
        try:
            await adapter.connect()
            break
        except BrokerError as exc:
            log.warning("connect failed (%s); retrying in %.0fs", exc, delay)
            # Keep the heartbeat going: "engine running, IB disconnected".
            _heartbeat(adapter, connect_error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    while not stop.is_set():
        try:
            await _ensure_connected(adapter, halts)
            reconcile_state, last_reconcile = await _cycle(
                adapter, journal, halts, margin, last_reconcile, reconcile_state, budget
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Spec §6.4: an unhandled engine error halts opening; keep running.
            log.exception("engine cycle failed")
            _trigger_once(halts, f"engine error: {type(exc).__name__}: {exc}")
        halt = halts.state()
        reading = margin.last
        _heartbeat(
            adapter,
            margin=reading.to_dict() if reading else None,
            budget=budget.last,
            halted=halt.active,
            halt_scope=halt.scope.value if halt.active else None,
            halt_reason=halt.reason if halt.active else "",
            **reconcile_state,
        )
        if once:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

    await adapter.disconnect()
    journal.close()
    return 0


def _trigger_once(halts, reason: str, scope=None) -> None:
    """Trigger a halt unless the same reason is already recorded (no log flood)."""
    state = halts.state()
    if state.active and reason in state.reason:
        return
    if scope is None:
        halts.trigger(reason)
    else:
        halts.trigger(reason, scope)


async def _ensure_connected(adapter, halts) -> None:
    """Reconnect if the adapter's own reconnect loop has given up."""
    if adapter.is_connected or getattr(adapter, "reconnecting", False):
        return
    try:
        await adapter.connect()
        log.info("reconnected to the broker")
    except BrokerError as exc:
        log.warning("still disconnected: %s", exc)
        if type(exc).__name__ == "AccountMismatch":
            _trigger_once(halts, f"account mismatch: {exc}")


async def _cycle(
    adapter, journal, halts, margin, last_reconcile, reconcile_state, budget=None
):
    """One monitor pass: account and margin, positions, reconciliation, budget."""
    snap = None
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
    margin.update(snap, adapter.daily_pnl(), adapter.pnl_at_account_update())
    try:
        positions = await adapter.positions()
        prev_record = journal.latest("positions_snapshot")
        previous = (
            {p.contract.key: p.quantity for p in prev_record.data}
            if prev_record
            else None
        )
        journal.append("positions_snapshot", tuple(positions), key=adapter.account_id)
        result = reconcile(
            expected_from_journal(journal), positions, utcnow().date(), previous
        )
        summary = result_to_dict(result)
        if summary != last_reconcile:  # journal changes only, not every loop
            journal.append("reconciliations", summary, key=adapter.account_id)
            last_reconcile = summary
        for m in result.mismatches:
            log.error(
                "RECONCILE MISMATCH %s: bot expects %g, IB has %g (%s)",
                m.key,
                m.expected,
                m.actual,
                m.hint,
            )
        if result.should_halt:
            # Spec §6.4: a reconciliation mismatch halts all trading.
            keys = ", ".join(m.key for m in result.mismatches)
            _trigger_once(halts, f"reconcile mismatch: {keys}", HaltScope.ALL)
        reconcile_state = {
            "reconcile_ok": result.ok,
            "mismatches": len(result.mismatches),
            "unmanaged": len(result.unmanaged),
        }
        if budget is not None:
            used = await budget.update(adapter, positions, snap)
            if used and used["over_budget"]:
                log.warning(
                    "RISK BUDGET exceeded: options loss $%.0f in -20%%/IV+50%% vs budget $%.0f (%s)",
                    used["used"],
                    used["budget"],
                    used["regime"],
                )
    except BrokerError as exc:
        log.warning("no positions: %s", exc)
    return reconcile_state, last_reconcile


def _on_margin_change(journal, account_id, new: MarginReading, old) -> None:
    """Journal and log every margin state change (alerts are sent in T20)."""
    journal.append("margin_events", new.to_dict(), key=account_id, ts=new.ts)
    previous = old.state.value if old else "start"
    message = (
        f"MARGIN {previous} -> {new.state.value}: EL ${new.excess_liquidity or 0:,.0f}, "
        f"cushion {(new.cushion or 0):.1%}" + (f" ({new.reason})" if new.reason else "")
    )
    if new.state.value in ("breach", "emergency"):
        log.error(message)
    elif new.state.value == "normal":
        log.info(message)
    else:
        log.warning(message)


def _heartbeat(adapter, **extra) -> None:
    health = getattr(adapter, "health", None)
    write_heartbeat(
        HEARTBEAT_FILE,
        connected=adapter.is_connected,
        read_only=adapter.read_only,
        live=getattr(adapter, "is_live", False),
        account_stale=adapter.account_is_stale(),
        last_error=getattr(health, "last_error", ""),
        **extra,
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
