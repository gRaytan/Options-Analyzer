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
from typing import Optional

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
from src.trading import rolls
from src.trading import spreads
from src.trading import rebuild
from src.trading import hedge
from src.trading.domain import HistoryKind
from src.trading.scanner import ScanResult, report_strategies
from dataclasses import replace
from src.trading.assignments import handle_assignment
from src.trading import margin_restoration
from src.trading.approvals import ApprovalQueue
from src.trading.executor import Executor
from src.trading.mode_policy import connection_read_only, live_allowed
from src.trading.order_manager import ExecStatus, OrderManager
from src.trading.position_manager import PositionManager
from src.trading import entries as entry_rules
from src.trading.history import HistoryStore
from src.trading.scanner import scan as run_scan
from src.trading.tracker import Tracker
from src.trading.config import StrategyName
from src.trading.broker.ib_adapter import is_paper_account
from src.trading.calendars import EventCalendar
from src.trading.domain import OptionContract, StockContract
from src.trading.status import write_heartbeat
from src.trading import scheduler as sched
from src.trading.calendars import MarketCalendar

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
    config=None,
    order_sleep=None,
) -> int:
    """Run the read-only monitor loop. ``adapter``/``journal`` are injectable for
    tests; ``config`` and ``order_sleep`` (the order ladder's wait) for backtests."""
    global _last_guard  # pylint: disable=global-statement
    _last_guard = []
    config = config if config is not None else load_trading_config()
    if adapter is None and events is None:
        events = EventCalendar()  # production: earnings / ex-dividend dates
    if adapter is None:
        if not config.account_id:
            log.error("TRADING_ACCOUNT_ID is not set")
            return 2
        settings = IBConnectionSettings.from_env()
        is_live = not is_paper_account(config.account_id) or not settings.is_paper_port
        adapter = IBAdapter(
            config.account_id,
            settings,
            read_only=connection_read_only(config, is_live),
            allow_live=live_allowed(config),
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
    executor = _make_executor(
        adapter, journal, config, halts, notifier, budget, sleep=order_sleep
    )
    entry_ctx = EntryContext(halts, budget, events, journal)

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

    # Spec §8: jobs on the NYSE calendar. A one-shot run (tests, backtests)
    # runs every job once.
    schedule = None if once else sched.Scheduler(MarketCalendar())
    hb_state: dict = {}
    while not stop.is_set():
        now = utcnow()
        jobs = None if schedule is None else schedule.due(now)
        if jobs is not None and not jobs:
            # Nothing due: keep the heartbeat fresh (watchdog, health check)
            # with the last cycle's state.
            _heartbeat(adapter, **hb_state)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                pass
            continue
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
                executor,
                entry_ctx,
                jobs=jobs,
            )
            if schedule is not None:
                schedule.mark(jobs, now)
        except Exception as exc:  # pylint: disable=broad-except
            # Spec §6.4: an unhandled engine error halts opening; keep running.
            log.exception("engine cycle failed")
            _trigger_once(
                halts, f"engine error: {type(exc).__name__}: {exc}", notifier=notifier
            )
        halt = halts.state()
        reading = margin.last
        hb_state = dict(
            margin=reading.to_dict() if reading else None,
            budget=budget.last,
            guard=_last_guard,
            halted=halt.active,
            halt_scope=halt.scope.value if halt.active else None,
            halt_reason=halt.reason if halt.active else "",
            **reconcile_state,
        )
        _heartbeat(adapter, **hb_state)
        if once:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

    await _flush_alerts()
    executor.orders.close()
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
    executor=None,
    entry_ctx=None,
    jobs=None,
):
    """One monitor pass: account and margin, positions, reconciliation, budget,
    then acting on what was found (T28).

    ``jobs`` (T38): the scheduled jobs due now; ``None`` runs everything. A
    margin breach always runs the full management pass (restoration)."""
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
    reading = margin.last
    breach = reading is not None and reading.state.value in ("breach", "emergency")
    if jobs is None:
        observe = act = scan = True
    else:
        observe = breach or bool(jobs & OBSERVE_JOBS)
        act = breach or bool(jobs & ACT_JOBS)
        scan = sched.SCAN in jobs
    if not observe:
        return reconcile_state, last_reconcile
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
        for a in result.assignments:
            # T28c: an assignment that happened anyway. Journal it, plan the undo.
            outcome = handle_assignment(
                a, journal, config or load_trading_config(), utcnow().date()
            )
            if outcome.intent is not None:
                journal.append("intents", outcome.intent, key=outcome.intent.intent_id)
            log.error("ASSIGNMENT %s", outcome.message)
            _alert(
                notifier,
                "critical",
                f"assignment:{a.option_key}",
                "Option assigned",
                outcome.message + "\n(Orders are sent once mode gating is enabled.)",
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
            "assignments": len(result.assignments),
        }
        if config is not None:
            await _check_guard(
                adapter, positions, config, events, notifier, journal, snap
            )
        if budget is not None:
            before = budget.last or {}
            used = await budget.update(adapter, positions, snap)
            if used:
                _drawdown_alert(notifier, before, used, config)
            crash = (used or {}).get("crash")
            if crash and not crash["survives"]:
                _alert(
                    notifier,
                    "warning",
                    "crash-test",
                    f"Account fails the {crash['move']:+.0%} crash test",
                    f"If the S&P 500 fell {abs(crash['move']):.0%} (each stock by its beta, "
                    f"IV +50%, margin x1.5): NLV ${crash['nlv_after']:,.0f}, Excess "
                    f"Liquidity ${crash['excess_liquidity_after']:,.0f}, below the "
                    f"${config.margin_floor.excess_liquidity_min if config else 100_000:,.0f} "
                    "floor. No new risk is added; consider reducing leverage or short puts.",
                )
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
        if jobs is not None and sched.PREOPEN in jobs:
            _preopen(adapter, config, events, positions, journal, notifier)
        if act and executor is not None and config is not None:
            if entry_ctx is not None:
                entry_ctx.scheduled = jobs is not None
            await _act(
                executor,
                journal,
                config,
                margin,
                positions,
                snap,
                notifier,
                entry_ctx if scan else None,
                rebuild_ctx=entry_ctx,
            )
        if jobs is not None and sched.EXPIRY_SWEEP in jobs:
            _expiry_sweep(positions, journal, notifier)
        if jobs is not None and sched.OPPORTUNITIES in jobs:
            _opportunities_digest(journal, config, notifier)
        if jobs is not None and sched.EOD in jobs:
            _daily_report(journal, snap, budget, margin, positions, notifier)
    except BrokerError as exc:
        log.warning("no positions: %s", exc)
    return reconcile_state, last_reconcile


def _drawdown_alert(notifier, before: dict, now: dict, config) -> None:
    """Alert when the options drawdown halves the budget, halts new risk, or clears."""

    def level(d):
        return (
            "halted"
            if d.get("halted")
            else ("reduced" if d.get("reduced") else "normal")
        )

    old, new = level(before), level(now)
    if old == new or (not before and new == "normal"):
        return
    dd = now.get("drawdown") or 0.0
    risk = config.risk if config else None
    text = {
        "halted": (
            "critical",
            f"Options drawdown {dd:.1%}: no new trades",
            f"The options book is {dd:.1%} of NLV below its best point "
            f"(stop at {risk.options_dd_halt:.0%}). The bot opens nothing new and "
            "only manages what's open, until the drawdown is back under "
            f"{risk.options_dd_recover:.0%}."
            if risk
            else "",
        ),
        "reduced": (
            "warning",
            f"Options drawdown {dd:.1%}: risk halved",
            f"The options book is {dd:.1%} of NLV below its best point "
            f"(halve at {risk.options_dd_reduce:.0%}): new trades get half the usual "
            "risk budget."
            if risk
            else "",
        ),
        "normal": (
            "info",
            "Options drawdown recovered",
            f"The options drawdown is {dd:.1%}: the risk budget is back to normal.",
        ),
    }[new]
    _alert(notifier, text[0], "options-drawdown", text[1], text[2])


SCAN_EVERY_S = 15 * 60  # spec §8: the scanner runs every 15 minutes
OBSERVE_JOBS = frozenset(
    {
        sched.MANAGE,
        sched.SCAN,
        sched.PREOPEN,
        sched.EXPIRY_SWEEP,
        sched.EOD,
        sched.OPPORTUNITIES,
    }
)
ACT_JOBS = frozenset({sched.MANAGE, sched.SCAN, sched.EXPIRY_SWEEP})


def _preopen(adapter, config, events, positions, journal, notifier) -> None:
    """09:00 ET: warm the event calendars, check the connection and permissions."""
    symbols = sorted(
        {
            (
                p.contract.symbol
                if isinstance(p.contract, StockContract)
                else p.contract.underlying
            )
            for p in positions
        }
        | set((config.watchlist if config else {}).keys())
    )
    loaded = 0
    if events is not None:
        for sym in symbols:
            try:
                events.events(sym)
                loaded += 1
            except Exception as exc:  # pylint: disable=broad-except
                log.warning("pre-open: events for %s unavailable: %s", sym, exc)
    detail = {
        "connected": adapter.is_connected,
        "read_only": adapter.read_only,
        "mode": config.mode.value if config else None,
        "events_loaded": loaded,
        "symbols": symbols,
    }
    journal.append("job_runs", {"job": sched.PREOPEN, **detail}, key=sched.PREOPEN)
    log.info("PRE-OPEN %s", detail)
    if config is not None and config.mode.value != "shadow" and adapter.read_only:
        _alert(
            notifier,
            "warning",
            "preopen:read-only",
            "Bot is read-only",
            f"Mode is {config.mode.value} but the IB connection is read-only: "
            "no orders can be sent today.",
        )


def _expiry_sweep(positions, journal, notifier) -> None:
    """15:00 ET: list everything expiring today (the guards act on the risky ones)."""
    today = utcnow().astimezone(sched.ET).date()
    expiring = [
        p
        for p in positions
        if isinstance(p.contract, OptionContract) and p.contract.expiry == today
    ]
    journal.append(
        "job_runs",
        {"job": sched.EXPIRY_SWEEP, "expiring": [p.contract.key for p in expiring]},
        key=sched.EXPIRY_SWEEP,
    )
    if not expiring:
        return
    planned = {g["key"]: g.get("plan", "") for g in _last_guard}
    lines = [
        f"{p.contract.key} x{p.quantity:g}: {planned.get(p.contract.key) or 'expires (no guard trigger)'}"
        for p in expiring
    ]
    _alert(
        notifier,
        "warning" if planned.keys() & {p.contract.key for p in expiring} else "info",
        f"expiry:{today}",
        f"Expiring today: {len(expiring)}",
        "\n".join(lines),
    )


def _daily_report(journal, snap, budget, margin, positions, notifier) -> dict:
    """16:15 ET: one message with the day's numbers (spec §8, §9)."""
    now = utcnow()
    start = now.astimezone(sched.ET).replace(hour=0, minute=0, second=0, microsecond=0)
    fills = [r.data for r in journal.read("fills", since=start)]
    sold = sum(
        f.price * f.quantity * f.contract.multiplier
        for f in fills
        if isinstance(f.contract, OptionContract) and f.action.sign < 0
    )
    bought = sum(
        f.price * f.quantity * f.contract.multiplier
        for f in fills
        if isinstance(f.contract, OptionContract) and f.action.sign > 0
    )
    fees = sum(f.commission for f in fills)
    options = [p for p in positions if isinstance(p.contract, OptionContract)]
    reading = margin.last
    b = (budget.last if budget is not None else None) or {}
    report = {
        "job": sched.EOD,
        "date": start.date().isoformat(),
        "nlv": snap.net_liquidation if snap else None,
        "excess_liquidity": snap.excess_liquidity if snap else None,
        "cushion": snap.cushion if snap else None,
        "margin_state": reading.state.value if reading else "unknown",
        "regime": b.get("regime"),
        "budget_used": b.get("used"),
        "budget": b.get("budget"),
        "fills": len(fills),
        "premium_sold": sold,
        "premium_bought": bought,
        "fees": fees,
        "open_options": len(options),
    }
    journal.append("job_runs", report, key=sched.EOD)
    el = f"${report['excess_liquidity']:,.0f}" if snap else "n/a"
    cushion = f"{report['cushion']:.0%}" if snap else "n/a"
    budget_txt = (
        f"${b['used']:,.0f} of ${b['budget']:,.0f} ({b.get('regime')})" if b else "n/a"
    )
    _alert(
        notifier,
        "report",
        f"daily:{report['date']}",
        f"Daily report {report['date']}",
        f"NLV ${report['nlv'] or 0:,.0f}, EL {el}, Cushion {cushion}, margin {report['margin_state']}.\n"
        f"Risk budget used {budget_txt}.\n"
        f"Today: {len(fills)} fills, premium sold ${sold:,.0f}, bought ${bought:,.0f}, fees ${fees:,.2f}.\n"
        f"Open option positions: {len(options)}.",
    )
    return report


class EntryContext:
    """What the entry step needs across cycles (T33)."""

    def __init__(self, halts, budget, events, journal, history=None, tracker=None):
        self.halts = halts
        self.budget = budget
        self.events = events
        self.history = history
        self.tracker = tracker or Tracker(journal)
        self.last_scan = None  # datetime of the last scan
        self.scheduled = False  # True when the scheduler decides when to scan


def _make_executor(
    adapter, journal, config, halts, notifier, budget, sleep=None
) -> Executor:
    steps = {s: e.order_step_s for s, e in config.watchlist.items() if e.order_step_s}
    kw = {"sleep": sleep} if sleep is not None else {}
    orders = OrderManager(
        adapter, journal, step_overrides=steps, allow_live=live_allowed(config), **kw
    )

    def on_request(data: dict) -> None:
        _alert(
            notifier,
            "warning",
            f"approval:{data['intent_id']}",
            "Approval needed",
            f"{data['summary']}\nApprove or reject on the Approvals page within 10 minutes.",
        )

    approvals = ApprovalQueue(journal, on_request=on_request)

    def regime_multiplier() -> float:
        return (
            budget.last["multiplier"] if budget.last else 0.0
        )  # unknown = no new risk

    return Executor(
        adapter,
        journal,
        config,
        halts,
        orders,
        approvals,
        regime_multiplier=regime_multiplier,
    )


def _done(journal, intent_id: str) -> bool:
    return any(
        r.data.get("event") == "result"
        and r.data.get("status") in ("filled", "already_done")
        for r in journal.read("order_events", key=intent_id)
    )


async def _act(
    executor,
    journal,
    config,
    margin,
    positions,
    snap,
    notifier,
    entry_ctx=None,
    rebuild_ctx=None,
) -> None:
    """Act on this cycle's findings, through the mode policy (T28)."""
    if snap is None:
        return
    today = utcnow().date()
    shorts = [
        p
        for p in positions
        if isinstance(p.contract, OptionContract) and p.quantity < 0
    ]
    try:
        quotes = await executor.quotes([p.contract for p in shorts]) if shorts else {}
    except BrokerError as exc:
        log.warning("act: no quotes (%s)", exc)
        quotes = {}

    # 1. Margin restoration comes first; during a breach nothing else runs.
    reading = margin.last
    if reading is not None and reading.state.value in ("breach", "emergency"):
        flagged = {g["key"] for g in _last_guard}
        est = (
            reading.excess_liquidity
            if reading.excess_liquidity is not None
            else snap.excess_liquidity
        )
        plan = margin_restoration.plan(positions, quotes, snap, est, config, flagged)
        if plan.closes:
            result = await margin_restoration.restore(
                plan,
                executor.submit,
                emergency=reading.state.value == "emergency",
                today=today,
            )
            _alert(
                notifier,
                "critical",
                "restoration",
                "Margin restoration",
                f"Need ${plan.need:,.0f} of Excess Liquidity; planned {len(plan.closes)} close(s), "
                f"filled so far ${result.gained:,.0f}.",
            )
        elif plan.need > 0:
            _alert(
                notifier,
                "critical",
                "restoration-none",
                "Margin breach: nothing to close",
                f"Need ${plan.need:,.0f} of Excess Liquidity, but no naked short option can be "
                "closed to free it. Action needed.",
            )
        return

    # 2. Guard rolls and closes planned this cycle (T28b).
    book = rolls.ChainBook(journal)
    for plan in list(_last_roll_plans.values()):
        res = await executor.submit(plan.intent)
        if (
            res is not None
            and res.status is ExecStatus.REJECTED
            and plan.action == "roll"
        ):
            # The roll itself was refused (e.g. the new leg would break a cap):
            # don't stay in the risky position, close it instead (T37 finding).
            plan = rolls.close_instead(plan, f"roll rejected: {res.detail}", today)
            res = await executor.submit(plan.intent)
        if res is not None and res.status is ExecStatus.FILLED:
            if plan.action == "roll":
                book.record_roll(
                    plan.chain, plan.candidate.new.key, -(res.avg_price or 0.0)
                )
            else:
                book.record_close(plan.chain, res.avg_price or 0.0)

    # 3. Position manager exits for the bot's own positions (T25).
    # A spread's short leg closes together with its long leg (T34).
    managed = set(expected_from_journal(journal))
    pairs = spreads.pair_spreads(positions, managed)
    for decision in PositionManager(config).review(
        positions, quotes, snap, managed, today
    ):
        await executor.submit(spreads.as_spread_close(decision.intent, pairs))

    # 4. Undo pending assignments (T28c).
    pending = {}
    for rec in journal.read(
        "intents"
    ):  # the order manager journals intents too: dedupe
        if rec.data.strategy == "assignment_handler":
            pending[rec.key] = rec.data
    for intent_id, intent in pending.items():
        if not _done(journal, intent_id):
            await executor.submit(intent)

    # 5. The opportunities report (every scan tick, whatever the mode), then
    #    new entries from it (T33), only when everything allows new risk.
    if entry_ctx is not None:
        scanned = await _opportunities(
            executor, journal, config, positions, snap, entry_ctx, today
        )
        await _enter(
            executor,
            journal,
            config,
            margin,
            positions,
            snap,
            entry_ctx,
            today,
            scanned=scanned,
        )

    # 6. The owner's rebuild plan: build it on request, then submit its items;
    #    each one waits for the owner's approval (rebuild.py).
    await _rebuild(
        executor, journal, config, margin, positions, snap, rebuild_ctx, today
    )

    # 7. The crash hedge (collar): planned once a day, each order approved by you.
    if config.enabled_strategies[StrategyName.TAIL_HEDGE].open:
        await _hedge(executor, journal, config, positions, snap, today)


async def _opportunities(executor, journal, config, positions, snap, ctx, today):
    """Scan the real market for the report (Opportunities page, daily digest)."""
    budget = ctx.budget.last if ctx.budget is not None else None
    try:
        result = await _scan(
            executor,
            journal,
            config,
            positions,
            snap,
            ctx,
            budget,
            today,
            strategies=report_strategies(config),
        )
    except BrokerError as exc:
        log.warning("opportunities scan: market data unavailable (%s)", exc)
        return None
    ctx.last_scan = utcnow()
    log.info(
        "opportunities: %d candidates, %d ready to sell",
        len(result.candidates),
        len(result.ready()),
    )
    return result


def _opportunities_digest(journal, config, notifier) -> Optional[dict]:
    """10:00 ET: the day's best candidates from the latest scan, in one message."""
    today = utcnow().astimezone(sched.ET).date().isoformat()
    rec = journal.latest("scan_results")
    data = rec.data if rec is not None and rec.data.get("date") == today else None
    if data is None:
        body = "No scan yet today: is the engine connected to IB with market data?"
        best = []
    else:
        best = data.get("best", [])[:5]
        lines = []
        for c in best:
            tag = "READY" if c.get("fire") else "wait"
            delta = c.get("delta")
            lines.append(
                f"{tag:5s} {c['strategy'].replace('_', ' ')}: {c['key']}"
                + (f" (delta {abs(delta):.2f})" if delta is not None else "")
                + f", credit ${c.get('net_credit', 0):,.0f}/contract,"
                f" yield {c.get('annual_yield', 0):.0%}/yr"
            )
        if not lines:
            top = sorted(data.get("rejected", {}).items(), key=lambda kv: -kv[1])[:3]
            lines.append(
                "Nothing passes the filters today. Most common reasons: "
                + "; ".join(f"{k} ({v})" for k, v in top)
            )
        body = (
            f"VIX regime: {data.get('regime', 'unknown')}.\n"
            + "\n".join(lines)
            + (
                "\nReport only (not enabled to open): " + ", ".join(data["report_only"])
                if data.get("report_only")
                else ""
            )
            + "\nDetails on the Opportunities page."
        )
    report = {"job": sched.OPPORTUNITIES, "date": today, "best": best}
    journal.append("job_runs", report, key=sched.OPPORTUNITIES)
    _alert(notifier, "report", f"opps:{today}", f"Opportunities {today}", body)
    return report


async def _enter(
    executor, journal, config, margin, positions, snap, ctx, today, scanned=None
) -> None:
    opening = [
        StrategyName.COVERED_CALL,
        StrategyName.INDEX_PUT,
        StrategyName.STOCK_PUT,
        StrategyName.PUT_SPREAD,
    ]
    if not any(config.enabled_strategies[s].open for s in opening):
        return
    reading = margin.last
    budget = ctx.budget.last if ctx.budget is not None else None
    if reading is None or not reading.allows_opening:
        return  # margin state must be normal
    if not ctx.halts.allows_opening():
        return
    if not budget or budget.get("multiplier", 0) <= 0:
        return  # unknown or rising VIX regime: no new risk
    if scanned is not None:
        # This tick's opportunities scan: entries only from strategies that may open.
        result = ScanResult(
            [
                c
                for c in scanned.candidates
                if config.enabled_strategies[c.strategy].open
            ],
            scanned.rejected,
        )
    else:
        now = utcnow()
        if (
            not ctx.scheduled  # the scheduler already paces the scan (T38)
            and ctx.last_scan is not None
            and (now - ctx.last_scan).total_seconds() < SCAN_EVERY_S
        ):
            return
        ctx.last_scan = now
        result = await _scan(
            executor, journal, config, positions, snap, ctx, budget, today
        )
    intents = entry_rules.entry_intents(
        result,
        budget,
        config,
        journal,
        today,
        positions=positions,
        nlv=snap.net_liquidation,
        excess_liquidity=snap.excess_liquidity,
    )
    log.info(
        "scan: %d candidates, %d ready, %d entries",
        len(result.candidates),
        len(result.ready()),
        len(intents),
    )
    for intent in intents:
        journal.append("intents", intent, key=intent.intent_id)
        await executor.submit(intent)


async def _scan(
    executor, journal, config, positions, snap, ctx, budget, today, strategies=None
):
    if ctx.history is None:
        cache = os.path.join(
            os.environ.get("TRADING_CACHE_DIR", "data/trading/cache"), "history"
        )
        ctx.history = HistoryStore(executor.broker, cache)
    return await run_scan(
        executor.broker,
        ctx.history,
        positions,
        snap,
        config,
        today,
        events=ctx.events,
        journal=journal,
        tracker=ctx.tracker,
        regime=(budget or {}).get("regime", "unknown"),
        strategies=strategies,
    )


def _todays_hedge(journal, today):
    for rec in reversed(journal.read("job_runs", key="hedge")):
        if rec.data.get("date") == today.isoformat():
            return rec.data
    return None


async def _hedge(executor, journal, config, positions, snap, today):
    """Plan today's hedge orders once, then submit them (each waits for approval)."""
    plan_data = _todays_hedge(journal, today)
    if plan_data is None:
        try:
            result = await _build_hedge(executor, config, positions, snap, today)
        except BrokerError as exc:
            log.warning("hedge: market data unavailable (%s); retrying", exc)
            return
        plan_data = {**result.to_dict(), "date": today.isoformat()}
        journal.append("job_runs", plan_data, key="hedge")
        log.info(
            "HEDGE coverage %.0f%%: %d orders planned",
            result.need.coverage * 100,
            len(result.intents),
        )
    for intent in plan_data.get("intents", []):
        if not _done(journal, intent.intent_id):
            await executor.submit(intent)


async def _build_hedge(executor, config, positions, snap, today):
    h = config.tail_hedge
    broker = executor.broker
    bars = await broker.daily_history(h.put_root, HistoryKind.PRICE, 10)
    index_price = bars[-1].close if bars else 0.0
    chains = await broker.option_chain(h.put_root)
    chain = next(
        (c for c in chains if c.trading_class.upper() == h.put_root.upper()),
        chains[0] if chains else None,
    )
    to_quote = []
    put = hedge.pick_put(chain, index_price, today, config) if chain else None
    if put is not None:
        to_quote.append(put)
    _, roll = hedge.hedge_puts(positions, config, today)
    to_quote += [p.contract for p in roll]
    put_quotes = await executor.quotes(to_quote) if to_quote else {}
    calls = {}
    if h.style == "collar":
        held = {
            p.contract.symbol
            for p in positions
            if isinstance(p.contract, StockContract) and p.quantity > 0
        }
        wanted = [s_ for s_ in h.call_symbols if s_ in held]
        # Strikes are set from the live price (never the shares' cost).
        live = (
            await executor.quotes([StockContract(s_) for s_ in wanted])
            if wanted
            else {}
        )
        prices = {}
        for q in live.values():
            px = q.mid if q.mid is not None else q.last
            if px:
                prices[q.contract.symbol] = px
        for sym in h.call_symbols:
            if (
                sym not in prices
                or hedge.uncovered_call_lots(positions, sym, h.call_coverage) <= 0
            ):
                continue
            sym_chains = await broker.option_chain(sym)
            if not sym_chains:
                continue
            call = hedge.pick_call(sym_chains[0], sym, prices[sym], today, config)
            if call is None:
                continue
            q = (await executor.quotes([call])).get(call.key)
            calls[sym] = (call, q)
    return hedge.plan(
        positions, snap, config, index_price, chain, put_quotes, calls, today, utcnow()
    )


async def _rebuild(executor, journal, config, margin, positions, snap, ctx, today):
    """Build the requested rebuild plan once, then submit its open items."""
    req = journal.latest("rebuild_requests")
    plan_rec = journal.latest("rebuild_plans")
    if req is not None and (
        plan_rec is None
        or plan_rec.data.get("request_id") != req.data.get("request_id")
    ):
        try:
            plan = await _build_rebuild_plan(
                executor, journal, config, margin, positions, snap, ctx, today, req.data
            )
        except BrokerError as exc:
            log.warning("rebuild plan: market data unavailable (%s); retrying", exc)
            return
        journal.append("rebuild_plans", plan.to_dict(), key=plan.plan_id)
        log.info("REBUILD plan %s: %d items", plan.plan_id, len(plan.intents))
        plan_rec = journal.latest("rebuild_plans")
    if plan_rec is None:
        return
    for intent in plan_rec.data.get("intents", []):
        if not _done(journal, intent.intent_id):
            await executor.submit(intent)


async def _build_rebuild_plan(
    executor, journal, config, margin, positions, snap, ctx, today, request
):
    targets = {k.upper(): float(v) for k, v in request.get("targets", {}).items()}
    now = utcnow()
    plan_id = rebuild.new_plan_id(request.get("request_id", ""), now)
    budget = ctx.budget.last if ctx is not None and ctx.budget is not None else None
    regime = (budget or {}).get("regime", "unknown")
    stock_quotes = await executor.quotes([StockContract(s) for s in targets])
    prices = {}
    for key, q in stock_quotes.items():
        px = q.mid if q.mid is not None else q.last
        if px:
            prices[q.contract.symbol] = px
    options = [p.contract for p in positions if isinstance(p.contract, OptionContract)]
    quotes = await executor.quotes(options) if options else {}
    steps, fit, notes = rebuild.plan_shares(
        positions, quotes, snap, prices, targets, config, regime, today
    )
    plan = rebuild.RebuildPlan(
        plan_id,
        request.get("request_id", ""),
        now,
        targets,
        snap.net_liquidation,
        snap.excess_liquidity,
        snap.cushion,
        sum(
            p.quantity * (prices.get(p.contract.symbol) or p.market_price or 0.0)
            for p in positions
            if isinstance(p.contract, StockContract) and p.quantity > 0
        ),
        regime,
        steps,
        fit,
        rebuild.stock_intents(plan_id, steps, config, now),
        notes,
    )
    reading = margin.last
    if not request.get("options", True):
        plan.notes.append("Options not requested in this plan.")
    elif ctx is None:
        plan.notes.append("Options skipped: the scanner isn't available in this run.")
    elif reading is None or not reading.allows_opening:
        plan.notes.append("Options skipped: margin isn't in the normal state.")
    else:
        result = await _scan(
            executor, journal, config, positions, snap, ctx, budget, today
        )
        forced = ScanResult(
            [replace(c, fire=True) for c in result.candidates], result.rejected
        )
        opts = entry_rules.entry_intents(
            forced,
            budget,
            config,
            journal,
            today,
            positions=positions,
            nlv=snap.net_liquidation,
            excess_liquidity=snap.excess_liquidity,
        )
        plan.intents += rebuild.option_intents(plan_id, opts, config)
        if not opts:
            plan.notes.append(
                "No option trade passes the filters right now (premium, liquidity, "
                "risk budget or VIX regime)."
            )
    return plan


_last_guard: list = []
GUARD_ALERT_LEVEL = {
    "critical": "critical",
    "high": "warning",
    "warning": "warning",
    "normal": "info",
}


async def _check_guard(
    adapter, positions, config, events, notifier, journal=None, snap=None
) -> None:
    """Assignment & Event Guard: detect roll triggers (T28a) and plan rolls (T28b).

    Alert-only until mode gating (T28) sends the orders."""
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
    today = utcnow().date()
    signals = assignment_guard.scan(shorts, quotes, today, config, events)
    plans = await _plan_rolls(
        adapter, shorts, signals, config, events, journal, snap, today, positions
    )
    _last_guard = [{**s.to_dict(), "plan": plans.get(s.key, "")} for s in signals]
    for sig in signals:
        log.warning(
            "GUARD %s %s: %s %s", sig.trigger, sig.key, sig.detail, sig.deadline
        )
        plan_text = plans.get(sig.key, "")
        _alert(
            notifier,
            GUARD_ALERT_LEVEL[sig.severity],
            f"guard:{sig.trigger}:{sig.key}",
            f"Roll needed: {sig.key}",
            f"{sig.trigger.replace('_', ' ')}: {sig.detail}"
            + (f". Act: {sig.deadline}" if sig.deadline else "")
            + (f"\nPlan: {plan_text}" if plan_text else "")
            + "\n(Orders are sent once mode gating is enabled.)",
        )


MAX_ROLL_PLANS_PER_CYCLE = 3  # each plan quotes a chain: keep IB pacing low


async def _plan_rolls(
    adapter, shorts, signals, config, events, journal, snap, today, positions=()
):
    """Plan a roll (or close) per triggered position (T28b). Not executed yet.

    A put spread's short leg is never rolled alone: the whole spread is
    closed as one combo instead (T34)."""
    _last_roll_plans.clear()  # never carry a plan over: signals are per cycle
    if journal is None or snap is None or not signals:
        return {}
    book = rolls.ChainBook(journal)
    by_key = {p.contract.key: p for p in shorts}
    pairs = spreads.pair_spreads(positions, set(expected_from_journal(journal)))
    plans = {}
    for sig in signals:
        if sig.key in plans or len(plans) >= MAX_ROLL_PLANS_PER_CYCLE:
            continue
        if sig.key in pairs:
            pos = by_key[sig.key]
            reason = f"put spread: close instead of rolling ({sig.trigger})"
            chain = book.ensure(pos)
            intent = spreads.close_intent(
                pos, pairs[sig.key], reason, today, chain_id=chain["chain_id"]
            )
            plan = rolls.RollPlan("close", reason, intent, chain=chain)
            plans[sig.key] = f"close: {reason}"
            _last_roll_plans[sig.key] = plan
            continue
        try:
            plan = await rolls.roll_for(
                adapter, by_key[sig.key], snap, config, book, today, sig.trigger, events
            )
        except (BrokerError, ValueError) as exc:
            plans[sig.key] = f"could not plan ({exc})"
            continue
        plans[sig.key] = f"{plan.action}: {plan.reason}"
        _last_roll_plans[sig.key] = plan
    return plans


_last_roll_plans: dict = {}  # key -> RollPlan from this cycle (executed by _act)


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
