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
from src.trading import rolls
from src.trading import spreads
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
    executor = _make_executor(adapter, journal, config, halts, notifier, budget)
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
                executor,
                entry_ctx,
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
):
    """One monitor pass: account and margin, positions, reconciliation, budget,
    then acting on what was found (T28)."""
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
        if executor is not None and config is not None:
            await _act(
                executor, journal, config, margin, positions, snap, notifier, entry_ctx
            )
    except BrokerError as exc:
        log.warning("no positions: %s", exc)
    return reconcile_state, last_reconcile


SCAN_EVERY_S = 15 * 60  # spec §8: the scanner runs every 15 minutes


class EntryContext:
    """What the entry step needs across cycles (T33)."""

    def __init__(self, halts, budget, events, journal, history=None, tracker=None):
        self.halts = halts
        self.budget = budget
        self.events = events
        self.history = history
        self.tracker = tracker or Tracker(journal)
        self.last_scan = None  # datetime of the last scan


def _make_executor(adapter, journal, config, halts, notifier, budget) -> Executor:
    steps = {s: e.order_step_s for s, e in config.watchlist.items() if e.order_step_s}
    orders = OrderManager(
        adapter, journal, step_overrides=steps, allow_live=live_allowed(config)
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
    executor, journal, config, margin, positions, snap, notifier, entry_ctx=None
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

    # 5. New entries from the scanner (T33), only when everything allows new risk.
    if entry_ctx is not None:
        await _enter(
            executor, journal, config, margin, positions, snap, entry_ctx, today
        )


async def _enter(
    executor, journal, config, margin, positions, snap, ctx, today
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
    now = utcnow()
    if (
        ctx.last_scan is not None
        and (now - ctx.last_scan).total_seconds() < SCAN_EVERY_S
    ):
        return
    ctx.last_scan = now
    if ctx.history is None:
        cache = os.path.join(
            os.environ.get("TRADING_CACHE_DIR", "data/trading/cache"), "history"
        )
        ctx.history = HistoryStore(executor.broker, cache)
    result = await run_scan(
        executor.broker,
        ctx.history,
        positions,
        snap,
        config,
        today,
        events=ctx.events,
        journal=journal,
        tracker=ctx.tracker,
        regime=budget.get("regime", "unknown"),
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
