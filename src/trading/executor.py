"""Executor: from intents to orders under mode gating (plan T28, spec §11).

Each intent is routed by :func:`mode_policy.route`:

* **log** - journaled as a shadow intent (``order_events`` "shadow"), nothing sent;
* **ask** - an approval request (T27); sent once approved, dropped if it expires;
* **send** - the order manager (T22), after the full risk gate:
    - sanity checks for every order (T17 + V1: halts, fresh live quotes, price
      in the market, no flips, closes must reduce, no duplicates);
    - for openings and rolls: IB what-if margin, the hard limits (T18) and the
      stress test with the regime-scaled budget (T19/T19b).

Orders go out only in market hours: 09:45-15:45 ET, and from 09:31 for margin
restoration (spec §6.5, §8).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional

from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.broker.base import BrokerAdapter, BrokerError
from src.trading.calendars import MarketCalendar
from src.trading.config import TradingConfig
from src.trading.domain import (
    Contract,
    IntentKind,
    OrderRequest,
    Quote,
    TradeIntent,
    utcnow,
)
from src.trading.journal import Journal
from src.trading.kill_switch import HaltManager
from src.trading.mode_policy import RESTORATION_STRATEGY, Route, route
from src.trading.order_manager import ExecStatus, ExecutionResult, OrderManager
from src.trading.risk.checks import CheckResult, GateDecision
from src.trading.risk.limits import LimitContext, exposures_from, gate_decision
from src.trading.risk.sanity import OrderContext, sanity_checks, working_contract_keys
from src.trading.risk.stress import stress_checks

log = logging.getLogger("trading.executor")

OPEN_DELAY = timedelta(minutes=15)  # no normal orders in the first/last 15 minutes
RESTORE_DELAY = timedelta(minutes=1)


def describe(intent: TradeIntent) -> str:
    legs = ", ".join(
        f"{leg.action.value.lower()} {leg.quantity} {leg.contract.key}"
        for leg in intent.legs
    )
    return f"{intent.kind.value.capitalize()} ({intent.strategy}): {legs} [{intent.trigger}]"


class Executor:
    def __init__(
        self,
        broker: BrokerAdapter,
        journal: Journal,
        config: TradingConfig,
        halts: HaltManager,
        orders: OrderManager,
        approvals: ApprovalQueue,
        calendar: Optional[MarketCalendar] = None,
        env: Optional[Dict[str, str]] = None,
        clock: Callable[[], datetime] = utcnow,
        regime_multiplier: Callable[[], float] = lambda: 1.0,
    ):
        self.broker = broker
        self.journal = journal
        self.config = config
        self.halts = halts
        self.orders = orders
        self.approvals = approvals
        self.calendar = calendar or MarketCalendar()
        self.env = env
        self._clock = clock
        self.regime_multiplier = regime_multiplier

    # -- market window -------------------------------------------------------------

    def in_window(self, intent: TradeIntent) -> bool:
        now = self._clock()
        session = self.calendar.session(now.date())
        if session is None:
            return False
        open_, close = session
        if intent.strategy == RESTORATION_STRATEGY:
            return open_ + RESTORE_DELAY <= now < close
        return open_ + OPEN_DELAY <= now < close - OPEN_DELAY

    # -- risk gate --------------------------------------------------------------------

    def _opening_orders_today(self) -> int:
        start = self._clock().replace(hour=0, minute=0, second=0, microsecond=0)
        opening_ids = {
            r.key
            for r in self.journal.read("intents", since=start)
            if r.data.kind in (IntentKind.OPEN, IntentKind.HEDGE)
            and not r.data.risk_reducing
        }
        return len(
            {
                r.key
                for r in self.journal.read("orders", since=start)
                if r.key in opening_ids
            }
        )

    async def gate(
        self, order: OrderRequest, intent: TradeIntent, quotes: Dict[str, Quote]
    ) -> GateDecision:
        positions = await self.broker.positions()
        held = {}
        for p in positions:
            held[p.contract.key] = held.get(p.contract.key, 0.0) + p.quantity
        working = working_contract_keys(await self.broker.open_orders())
        ctx = OrderContext(self._clock(), quotes, held, working, self.halts.state())
        checks = list(sanity_checks(order, intent, ctx).checks)
        if intent.risk_reducing:
            return GateDecision(tuple(checks))
        try:
            account = await self.broker.account_snapshot()
            impact = await self.broker.what_if(order)
        except (BrokerError, NotImplementedError) as exc:
            checks.append(
                CheckResult("what_if", False, f"no margin check available: {exc}")
            )
            return GateDecision(tuple(checks))
        all_quotes = dict(quotes)
        options = [
            p.contract
            for p in positions
            if p.contract.key not in all_quotes and hasattr(p.contract, "expiry")
        ]
        if options:
            try:
                all_quotes.update(
                    {q.contract.key: q for q in await self.broker.quotes(options)}
                )
            except BrokerError:
                pass  # missing deltas count at their worst case
        exposures = exposures_from(positions, all_quotes)
        limit_ctx = LimitContext.from_config(
            self.config,
            account=account,
            impact=impact,
            exposures=exposures,
            quotes=all_quotes,
            opening_orders_today=self._opening_orders_today(),
        )
        checks.extend(gate_decision(intent, limit_ctx, self.config).checks)
        checks.extend(
            stress_checks(
                intent,
                exposures,
                all_quotes,
                account,
                self.config,
                self._clock().date(),
                self.regime_multiplier(),
            ).checks
        )
        return GateDecision(tuple(checks))

    async def quotes(self, contracts: List[Contract]) -> Dict[str, Quote]:
        return {q.contract.key: q for q in await self.broker.quotes(contracts)}

    # -- routing ----------------------------------------------------------------------

    async def submit(
        self, intent: TradeIntent, emergency: bool = False
    ) -> Optional[ExecutionResult]:
        is_live = bool(getattr(self.broker, "is_live", False))
        where = route(intent, self.config, is_live, self.env)
        if where is Route.LOG:
            self.journal.append(
                "order_events",
                {
                    "event": "shadow",
                    "summary": describe(intent),
                    "mode": self.config.mode.value,
                },
                key=intent.intent_id,
            )
            return None
        if where is Route.ASK:
            status = self.approvals.status(intent.intent_id)
            if status in (ApprovalStatus.UNKNOWN, ApprovalStatus.EXPIRED):
                if status is ApprovalStatus.EXPIRED and intent.details.get("ask_once"):
                    return None  # a plan item the owner let expire: don't ask again
                ttl_s = intent.details.get("approval_ttl_s")
                ttl = timedelta(seconds=float(ttl_s)) if ttl_s else None
                self.approvals.request(intent, describe(intent), ttl)
                return None
            if status is not ApprovalStatus.APPROVED:
                return None  # pending or rejected
        if not self.in_window(intent):
            return None  # retried next cycle in market hours
        res = await self.orders.execute(
            intent, self.quotes, self.gate, emergency=emergency
        )
        log.info("%s -> %s %s", describe(intent), res.status.value, res.detail)
        return res

    async def submit_all(
        self, intents: Iterable[TradeIntent]
    ) -> List[Optional[ExecutionResult]]:
        return [await self.submit(i) for i in intents]
