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
from src.trading.notifier import Notifier
from src.trading.reconcile import expected_from_journal, reconcile, result_to_dict
from src.trading.risk.budget import BudgetTracker
from src.trading import guard as assignment_guard
from src.trading.calendars import EventCalendar
from src.trading.domain import OptionContract
from src.trading.status import write_heartbeat

log = logging.getLogger("trading.run")

HEARTBEAT_FILE = "data/trading/heartbeat.json"


_pending_alerts: set = set()


def _alert(notifier, level: str, key: str, title: str, message: str) -> None:
    """Send an alert without blocking the engine (fire-and-forget task)."""
    if notifier is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        notifier.notify(level, key, title, message)
        return
    task = loop.create_task(notifier.anotify(level, key, title, message))
    _pending_alerts.add(task)
    task.add_done_callback(_pending_alerts.discard)


async def _flush_alerts() -> None:
    if _pending_alerts:
        await asyncio.gather(*list(_pending_alerts), return_exceptions=True)


async def monitor(
    interval_s: float,
    once: bool = False,
    adapter=None,
    journal=None,
    halt_file=None,
    notifier=None,
    events=None,
) -> int:
    """Run the read-only monitor loop. ``adapter``/``journal`` are injectable for tests."""
    global _last_guard  # pylint: disable=global-statement
    _last_guard = []
    config = load_trading_config()
    if adapter is None and events is None:
        events = EventCalendar()  # production: earnings / ex-dividend dates
    if adapter is None:
        if not config.account_id:
            log.error("TRADING_ACCOUNT_ID is not set")
            return 2
        adapter = IBAdapter(
            config.account_id, IBConnectionSettings.from_env(), read_only=True
        )
    if journal is None:
        journal = Journal(os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH)))
    if notifier is None:
        notifier = Notifier(config.alerts)
    halts = HaltManager(
        journal,
        halt_file or os.environ.get("TRADING_HALT_FILE", str(DEFAULT_HALT_FILE)),
    )
    margin = MarginMonitor(config.margin_floor)
    margin.on_change(
        lambda new, old: _on_margin_change(
            journal, adapter.account_id, new, old, notifier
        )
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
    failures = 0
    while not stop.is_set():
        try:
            await adapter.connect()
            break
        except BrokerError as exc:
            failures += 1
            log.warning("connect failed (%s); retrying in %.0fs", exc, delay)
            if failures == 4:  # 5 + 10 + 20 s of retries: about half a minute
                _alert(
                    notifier,
                    "warning",
                    "ib-connect",
                    "Cannot reach IB",
                    f"The engine can't connect to IB Gateway: {exc}. "
                    "Check that Gateway is running and logged in (weekly 2FA).",
                )
            # Keep the heartbeat going: "engine running, IB disconnected".
            _heartbeat(adapter, connect_error=str(exc))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    while not stop.is_set():
        try:
            await _ensure_connected(adapter, halts, notifier)
            reconcile_state, last_reconcile = await _cycle(
                adapter,
                journal,
                halts,
                margin,
                last_reconcile,
                reconcile_state,
                budget,
                notifier,
                events,
                config,
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Spec §6.4: an unhandled engine error halts opening; keep running.
            log.exception("engine cycle failed")
            _trigger_once(
                halts, f"engine error: {type(exc).__name__}: {exc}", notifier=notifier
            )
        halt = halts.state()
        reading = margin.last
        _heartbeat(
            adapter,
            margin=reading.to_dict() if reading else None,
            budget=budget.last,
            guard=_last_guard,
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

    await _flush_alerts()
    await adapter.disconnect()
    journal.close()
    return 0


def _trigger_once(halts, reason: str, scope=None, notifier=None) -> None:
    """Trigger a halt unless the same reason is already recorded (no log flood)."""
    state = halts.state()
    if state.active and reason in state.reason:
        return
    if scope is None:
        new = halts.trigger(reason)
    else:
        new = halts.trigger(reason, scope)
    what = "everything" if new.scope is HaltScope.ALL else "new trades"
    _alert(
        notifier,
        "critical",
        f"halt:{reason[:60]}",
        f"Bot halted ({what})",
        f"{reason}\n\nCheck the Bot Status page; only you can resume.",
    )


async def _ensure_connected(adapter, halts, notifier=None) -> None:
    """Reconnect if the adapter's own reconnect loop has given up."""
    if adapter.is_connected or getattr(adapter, "reconnecting", False):
        return
    try:
        await adapter.connect()
        log.info("reconnected to the broker")
    except BrokerError as exc:
        log.warning("still disconnected: %s", exc)
        if type(exc).__name__ == "AccountMismatch":
            _trigger_once(halts, f"account mismatch: {exc}", notifier=notifier)


async def _cycle(
    adapter,
    journal,
    halts,
    margin,
    last_reconcile,
    reconcile_state,
    budget=None,
    notifier=None,
    events=None,
    config=None,
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
            _trigger_once(
                halts, f"reconcile mismatch: {keys}", HaltScope.ALL, notifier=notifier
            )
        reconcile_state = {
            "reconcile_ok": result.ok,
            "mismatches": len(result.mismatches),
            "unmanaged": len(result.unmanaged),
        }
        if config is not None:
            await _check_guard(adapter, positions, config, events, notifier)
        if budget is not None:
            used = await budget.update(adapter, positions, snap)
            if used and used["over_budget"]:
                _alert(
                    notifier,
                    "warning",
                    "budget",
                    "Risk budget exceeded",
                    f"Options loss in a -20%/IV+50% crash: ${used['used']:,.0f} "
                    f"vs budget ${used['budget']:,.0f} (regime {used['regime']}). "
                    "No new risk until it's back under budget.",
                )
                log.warning(
                    "RISK BUDGET exceeded: options loss $%.0f in -20%%/IV+50%% vs budget $%.0f (%s)",
                    used["used"],
                    used["budget"],
                    used["regime"],
                )
    except BrokerError as exc:
        log.warning("no positions: %s", exc)
    return reconcile_state, last_reconcile


_last_guard: list = []
GUARD_ALERT_LEVEL = {
    "critical": "critical",
    "high": "warning",
    "warning": "warning",
    "normal": "info",
}


async def _check_guard(adapter, positions, config, events, notifier) -> None:
    """Assignment & Event Guard (T28a): detect roll triggers; alert only for now."""
    global _last_guard  # pylint: disable=global-statement
    shorts = [
        p
        for p in positions
        if isinstance(p.contract, OptionContract) and p.quantity < 0
    ]
    quotes = {}
    if shorts:
        try:
            quotes = {
                q.contract.key: q
                for q in await adapter.quotes([p.contract for p in shorts])
            }
        except BrokerError as exc:
            log.warning("guard: no quotes (%s)", exc)
    signals = assignment_guard.scan(shorts, quotes, utcnow().date(), config, events)
    _last_guard = [s.to_dict() for s in signals]
    for sig in signals:
        log.warning(
            "GUARD %s %s: %s %s", sig.trigger, sig.key, sig.detail, sig.deadline
        )
        _alert(
            notifier,
            GUARD_ALERT_LEVEL[sig.severity],
            f"guard:{sig.trigger}:{sig.key}",
            f"Roll needed: {sig.key}",
            f"{sig.trigger.replace('_', ' ')}: {sig.detail}"
            + (f". Act: {sig.deadline}" if sig.deadline else "")
            + "\n(Automatic rolling arrives in a later version.)",
        )


MARGIN_ALERT_LEVEL = {
    "emergency": "critical",
    "breach": "critical",
    "warning": "warning",
    "unknown": "warning",
    "normal": "info",
}


def _on_margin_change(
    journal, account_id, new: MarginReading, old, notifier=None
) -> None:
    """Journal, log and alert every margin state change."""
    journal.append("margin_events", new.to_dict(), key=account_id, ts=new.ts)
    previous = old.state.value if old else "start"
    message = (
        f"MARGIN {previous} -> {new.state.value}: EL ${new.excess_liquidity or 0:,.0f}, "
        f"cushion {(new.cushion or 0):.1%}" + (f" ({new.reason})" if new.reason else "")
    )
    if old is not None or new.state.value != "normal":  # no alert for a normal start
        _alert(
            notifier,
            MARGIN_ALERT_LEVEL[new.state.value],
            f"margin:{new.state.value}",
            f"Margin {new.state.value}",
            message,
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
