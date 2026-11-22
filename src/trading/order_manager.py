"""Order manager: from an approved intent to a filled (or cancelled) order.

Plan T22a, spec §7.1–7.3. Broker-agnostic; the IB specifics are in the adapter.

For each intent:

1. **Guards.** Refuse on a read-only or live connection (live trading needs
   mode gating, T28). **Idempotency:** the order ref is the intent ID, so if a
   working order or fills already exist for it (e.g. after a restart), nothing
   new is sent.
2. **Risk gate.** The caller's gate function (sanity checks, limits, stress)
   must approve the first order. Every decision is journaled.
3. **Pricing ladder.** Start at the mid. Every ``step_s`` seconds (60; QQQM
   300), move 10% of the spread toward the natural price, re-quoting each
   time, for up to 4 steps, never beyond mid + 40% of the spread
   (emergency: up to the natural price).
4. **Timeout.** If still not (fully) filled after the last step, cancel. The
   caller retries next cycle; after 3 failed cycles it alerts and skips the
   day (tracked here per intent).

Every step goes to the journal: ``intents``, ``risk_checks``, ``orders``,
``order_events``, and every fill (deduplicated by execution ID), which is
what reconciliation (T13) reads.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Awaitable, Callable, Dict, List, Optional, Set

from src.trading.broker.base import (
    EVENT_FILL,
    BrokerAdapter,
    BrokerError,
    OrderState,
    OrderStatus,
)
from src.trading.domain import (
    Contract,
    Fill,
    OptionContract,
    OrderRequest,
    Quote,
    TradeIntent,
    utcnow,
)
from src.trading.journal import Journal
from src.trading.risk.checks import GateDecision
from src.trading.risk.sanity import net_market

log = logging.getLogger("trading.orders")

STEP_FRACTION = 0.10
MAX_STEPS = 4
WORST_FRACTION = 0.40
MAX_FAILED_CYCLES = 3

QuotesFn = Callable[[List[Contract]], Awaitable[Dict[str, Quote]]]
GateFn = Callable[
    [OrderRequest, TradeIntent, Dict[str, Quote]], Awaitable[GateDecision]
]


class ExecStatus(str, Enum):
    FILLED = "filled"
    PARTIAL = "partial"  # some filled, rest cancelled at timeout
    UNFILLED = "unfilled"  # nothing filled, cancelled at timeout
    REJECTED = "rejected"  # by the risk gate or the broker
    REFUSED = "refused"  # guards: read-only, live, skipped for the day
    ALREADY_DONE = "already_done"  # idempotency: fills or a working order exist


@dataclass
class ExecutionResult:
    intent_id: str
    status: ExecStatus
    filled: int = 0
    avg_price: Optional[float] = None
    detail: str = ""
    prices: List[float] = field(default_factory=list)  # limit prices tried


def tick_size(contract: Contract, price: float) -> float:
    """Minimum price increment. SPX/SPXW: $0.05 under $3, else $0.10.
    Others (SPY, QQQ, XSP and penny-program names): $0.01."""
    if isinstance(contract, OptionContract):
        root = (contract.trading_class or contract.underlying).upper()
        if root in ("SPX", "SPXW"):
            return 0.05 if abs(price) < 3 else 0.10
    return 0.01


def ladder(best: float, natural: float, emergency: bool = False) -> List[float]:
    """Net limit prices: mid, then 10% of the spread per step toward natural.

    Prices are net debits per unit (credits negative), so ``natural >= best``
    and moving toward natural means paying more / receiving less.
    """
    mid = (best + natural) / 2
    spread = natural - best
    cap = natural if emergency else mid + WORST_FRACTION * spread
    prices = [min(mid + k * STEP_FRACTION * spread, cap) for k in range(MAX_STEPS + 1)]
    if emergency:
        prices.append(natural)
    out: List[float] = []
    for p in prices:
        if not out or abs(p - out[-1]) > 1e-9:
            out.append(p)
    return out


def round_to_tick(price: float, tick: float, best: float, cap: float) -> float:
    """Nearest tick, but never worse than ``cap`` and never beyond ``best``."""
    rounded = round(price / tick) * tick
    if rounded > cap + 1e-9:
        rounded -= tick
    return round(max(rounded, best), 2)


class OrderManager:
    def __init__(
        self,
        broker: BrokerAdapter,
        journal: Journal,
        step_s: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        step_overrides: Optional[Dict[str, float]] = None,
        allow_live: bool = False,
    ):
        self.allow_live = allow_live
        self.broker = broker
        self.journal = journal
        self.step_s = step_s
        self.step_overrides = {k.upper(): v for k, v in (step_overrides or {}).items()}
        self._sleep = sleep
        self._fill_ids: Set[str] = {r.data.exec_id for r in journal.read("fills")}
        self.failed_cycles: Dict[str, int] = {}
        self._cap = 0.0  # worst allowed price for the current ladder
        self.skip_until: Dict[str, date] = {}
        broker.subscribe(EVENT_FILL, self._on_fill)

    # -- journaling -----------------------------------------------------------

    def _on_fill(self, fill: Fill) -> None:
        if fill.exec_id in self._fill_ids:
            return
        self._fill_ids.add(fill.exec_id)
        self.journal.append("fills", fill, key=fill.order_ref)

    def _event(self, intent_id: str, event: str, **data) -> None:
        self.journal.append("order_events", {"event": event, **data}, key=intent_id)

    # -- helpers --------------------------------------------------------------

    def _step_for(self, intent: TradeIntent) -> float:
        for leg in intent.legs:
            c = leg.contract
            sym = (c.underlying if isinstance(c, OptionContract) else c.symbol).upper()
            if sym in self.step_overrides:
                return self.step_overrides[sym]
        return self.step_s

    def _guard(self, intent: TradeIntent) -> Optional[str]:
        if self.broker.read_only:
            return "broker connection is read-only"
        if getattr(self.broker, "is_live", False) and not self.allow_live:
            return "live trading is not enabled (needs both keys; see mode_policy)"
        skip = self.skip_until.get(intent.intent_id)
        if skip and utcnow().date() <= skip:
            return f"skipped for today after {MAX_FAILED_CYCLES} unfilled cycles"
        return None

    async def _already_done(self, intent: TradeIntent) -> Optional[str]:
        filled = sum(
            f.quantity
            for f in await self.broker.executions()
            if f.order_ref == intent.intent_id
        )
        if filled >= intent.legs[0].quantity * len(intent.legs):
            return f"already filled ({filled} leg-contracts)"
        working = [
            s
            for s in await self.broker.open_orders()
            if s.order.order_ref == intent.intent_id
        ]
        if working:
            return "a working order already exists for this intent"
        return None

    def _price(
        self, intent: TradeIntent, quotes: Dict[str, Quote], step: int, emergency: bool
    ) -> Optional[float]:
        probe = OrderRequest(intent.intent_id, intent.legs, 0.0, intent.intent_id)
        market = net_market(probe, quotes)
        if market is None:
            return None
        best, natural = market
        prices = ladder(best, natural, emergency)
        raw = prices[min(step, len(prices) - 1)]
        tick = max(tick_size(leg.contract, raw) for leg in intent.legs)
        # Round the cap *up* to the next tick (at most the natural price), so a
        # one-tick-wide market can still trade; costs at most one tick.
        cap = min(math.ceil(max(prices) / tick - 1e-9) * tick, natural)
        # An intent may set a hard worst price, e.g. a roll that must still
        # collect a minimum credit: never pay more than that.
        worst = intent.details.get("worst_price")
        if worst is not None:
            cap = min(cap, float(worst))
            if cap < best - 1e-9:
                return None  # the market can't meet the intent's worst price
        self._cap = round(cap, 2)
        return round_to_tick(min(raw, cap), tick, best, cap)

    def _note_failure(self, intent_id: str) -> None:
        n = self.failed_cycles.get(intent_id, 0) + 1
        self.failed_cycles[intent_id] = n
        if n >= MAX_FAILED_CYCLES:
            self.skip_until[intent_id] = utcnow().date()
            log.warning("intent %s unfilled %d cycles: skipped for today", intent_id, n)

    # -- main entry -------------------------------------------------------------

    async def execute(
        self,
        intent: TradeIntent,
        quotes_fn: QuotesFn,
        gate_fn: GateFn,
        emergency: bool = False,
    ) -> ExecutionResult:
        iid = intent.intent_id
        refusal = self._guard(intent)
        if refusal:
            return ExecutionResult(iid, ExecStatus.REFUSED, detail=refusal)
        done = await self._already_done(intent)
        if done:
            return ExecutionResult(iid, ExecStatus.ALREADY_DONE, detail=done)

        self.journal.append("intents", intent, key=iid)
        contracts = [leg.contract for leg in intent.legs]
        quotes = await quotes_fn(contracts)
        price = self._price(intent, quotes, 0, emergency)
        if price is None:
            self._event(iid, "no_market")
            return ExecutionResult(
                iid, ExecStatus.REJECTED, detail="no two-sided market"
            )
        order = OrderRequest(iid, intent.legs, price, iid)
        decision = await gate_fn(order, intent, quotes)
        self.journal.append("risk_checks", decision.to_dict(), key=iid)
        if not decision.approved:
            self._event(iid, "gate_rejected", summary=decision.summary())
            return ExecutionResult(iid, ExecStatus.REJECTED, detail=decision.summary())

        self.journal.append("orders", order, key=iid)
        try:
            state = await self.broker.place_order(order)
        except BrokerError as exc:
            self._event(iid, "broker_error", error=str(exc))
            return ExecutionResult(iid, ExecStatus.REJECTED, detail=str(exc))
        prices = [price]
        self._event(iid, "placed", price=price, status=state.status.value)
        step_s = self._step_for(intent)

        step = 0
        while not state.status.is_done:
            await self._sleep(step_s)
            state = await self._refresh(iid, state)
            if state.status.is_done:
                break
            step += 1
            if step > MAX_STEPS + (1 if emergency else 0):
                break
            quotes = await quotes_fn(contracts)
            new_price = self._price(intent, quotes, step, emergency)
            if new_price is None:
                continue
            # Only ever step toward the natural price: never back off after a
            # re-quote (the new ladder's cap still bounds it).
            new_price = (
                max(new_price, prices[-1]) if new_price <= self._cap else self._cap
            )
            if abs(new_price - prices[-1]) < 1e-9:
                continue
            state = await self.broker.modify_order(iid, new_price)
            prices.append(new_price)
            self._event(
                iid, "repriced", price=new_price, step=step, status=state.status.value
            )

        if not state.status.is_done:
            state = await self.broker.cancel_order(iid)
            self._event(iid, "cancelled_timeout", filled=state.filled)

        return self._result(iid, state, prices)

    async def _refresh(self, iid: str, state: OrderState) -> OrderState:
        for s in await self.broker.open_orders():
            if s.order.order_ref == iid:
                return s
        # Not working any more: report fills from executions.
        fills = [f for f in await self.broker.executions() if f.order_ref == iid]
        qty = state.order.legs[0].quantity
        filled = min(
            qty, sum(f.quantity for f in fills) // max(1, len(state.order.legs))
        )
        status = (
            OrderStatus.FILLED
            if filled >= qty
            else (state.status if state.status.is_done else OrderStatus.CANCELLED)
        )
        return OrderState(state.order, status, filled, state.avg_price)

    def _result(
        self, iid: str, state: OrderState, prices: List[float]
    ) -> ExecutionResult:
        qty = state.order.legs[0].quantity
        if state.status is OrderStatus.FILLED or state.filled >= qty:
            self.failed_cycles.pop(iid, None)
            status = ExecStatus.FILLED
        elif state.filled > 0:
            status = ExecStatus.PARTIAL
        elif state.status is OrderStatus.REJECTED:
            status = ExecStatus.REJECTED
        else:
            status = ExecStatus.UNFILLED
            self._note_failure(iid)
        self._event(
            iid,
            "result",
            status=status.value,
            filled=state.filled,
            avg_price=state.avg_price,
        )
        return ExecutionResult(
            iid, status, state.filled, state.avg_price, state.reason, prices
        )
