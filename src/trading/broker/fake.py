"""In-memory broker for tests and backtests (plan T05).

Scriptable: tests set quotes, account values and chains, then the adapter
fills marketable limit orders against those quotes, tracks positions and
cash, and publishes the same events as the real adapter. Helpers simulate
partial fills, rejects, disconnects and assignments.
"""

from __future__ import annotations

import itertools
from dataclasses import replace
from datetime import datetime
from typing import Callable, Dict, List, Optional

from src.trading.broker.base import (
    EVENT_ACCOUNT,
    EVENT_CONNECTION,
    EVENT_FILL,
    EVENT_ORDER,
    EVENT_POSITION,
    BrokerAdapter,
    BrokerDisconnected,
    BrokerError,
    OptionChain,
    OrderRejected,
    OrderState,
    OrderStatus,
)
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    HistoryKind,
    Action,
    Contract,
    Fill,
    MarginImpact,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Quote,
    StockContract,
    utcnow,
)

WhatIfFn = Callable[[OrderRequest, AccountSnapshot], MarginImpact]


def naive_what_if(order: OrderRequest, account: AccountSnapshot) -> MarginImpact:
    """Rough margin model: each short option leg adds 15% of strike notional."""
    added = 0.0
    for leg in order.legs:
        if isinstance(leg.contract, OptionContract) and leg.action is Action.SELL:
            added += 0.15 * leg.contract.strike * leg.contract.multiplier * leg.quantity
    equity = account.excess_liquidity + account.maint_margin
    return MarginImpact(
        equity_with_loan_after=equity,
        init_margin_after=account.init_margin + added,
        maint_margin_after=account.maint_margin + added,
    )


class FakeAdapter(BrokerAdapter):
    def __init__(
        self,
        account_id: str = "DU000000",
        read_only: bool = False,
        account: Optional[AccountSnapshot] = None,
        commission_per_contract: float = 0.65,
        what_if_fn: WhatIfFn = naive_what_if,
        clock: Callable[[], datetime] = utcnow,
    ):
        super().__init__(account_id, read_only)
        self._clock = clock
        self._connected = False
        self._account = account or AccountSnapshot(
            account_id, clock(), 1_000_000, 700_000, 300_000, 350_000, 650_000
        )
        self._quotes: Dict[str, Quote] = {}
        self._positions: Dict[str, Position] = {}
        self._chains: Dict[str, List[OptionChain]] = {}
        self._history: Dict[tuple, List[Bar]] = {}
        self.history_calls = 0
        self._orders: Dict[str, OrderState] = {}
        self._fills: List[Fill] = []
        self._exec_ids = itertools.count(1)
        self._reject_next: Optional[str] = None
        self.commission_per_contract = commission_per_contract
        self.what_if_fn = what_if_fn
        self.auto_fill = True
        self.pnl: Optional[float] = None  # scripted daily P&L

    # -- scripting helpers ----------------------------------------------------

    def set_quote(self, quote: Quote) -> None:
        self._quotes[quote.contract.key] = quote

    def set_account(self, **changes) -> None:
        self._account = replace(self._account, ts=self._clock(), **changes)
        self._publish(EVENT_ACCOUNT, self._account)

    def set_position(
        self, contract: Contract, quantity: float, avg_cost: float
    ) -> None:
        self._apply_position(contract, quantity, avg_cost, replace_qty=True)

    def set_chain(self, underlying: str, chains: List[OptionChain]) -> None:
        self._chains[underlying] = chains

    def set_history(self, symbol: str, kind: HistoryKind, bars: List[Bar]) -> None:
        self._history[(symbol.upper(), kind)] = sorted(bars, key=lambda b: b.day)

    def daily_pnl(self) -> Optional[float]:
        return self.pnl

    def reject_next_order(self, reason: str = "rejected by fake broker") -> None:
        self._reject_next = reason

    def drop_connection(self) -> None:
        self._connected = False
        self._publish(EVENT_CONNECTION, False)

    def fill_order(self, order_ref: str, quantity: int, price: float) -> OrderState:
        """Fill (part of) a working order at ``price`` per unit."""
        state = self._orders[order_ref]
        if state.status.is_done:
            raise BrokerError(f"order {order_ref} is {state.status.value}")
        quantity = min(quantity, state.remaining)
        for leg in state.order.legs:
            self._record_fill(order_ref, leg, quantity, price, state.order)
        filled = state.filled + quantity
        prev_value = (state.avg_price or 0.0) * state.filled
        avg = (prev_value + price * quantity) / filled
        status = (
            OrderStatus.FILLED
            if filled == state.order.legs[0].quantity
            else OrderStatus.PARTIAL
        )
        return self._set_order(
            replace(state, status=status, filled=filled, avg_price=avg)
        )

    def assign(self, option: OptionContract, contracts: int) -> None:
        """Simulate assignment of a short option (spec §5.11)."""
        pos = self._positions.get(option.key)
        if pos is None or pos.quantity > -contracts:
            raise BrokerError("can only assign an existing short option position")
        self._apply_position(option, contracts, 0.0)
        shares = contracts * option.multiplier
        stock = StockContract(option.underlying)
        sign = 1 if option.right.value == "P" else -1  # put: receive shares
        self._apply_position(stock, sign * shares, option.strike)
        self._adjust_cash(-sign * shares * option.strike)

    # -- connection ------------------------------------------------------------

    async def connect(self) -> None:
        self._connected = True
        self._publish(EVENT_CONNECTION, True)

    async def disconnect(self) -> None:
        self._connected = False
        self._publish(EVENT_CONNECTION, False)

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _require_connection(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("fake broker is disconnected")

    # -- data -----------------------------------------------------------------

    async def account_snapshot(self) -> AccountSnapshot:
        self._require_connection()
        return self._account

    async def positions(self) -> List[Position]:
        self._require_connection()
        return list(self._positions.values())

    async def quote(self, contract: Contract) -> Quote:
        self._require_connection()
        try:
            return self._quotes[contract.key]
        except KeyError:
            raise BrokerError(f"no quote for {contract.key}") from None

    async def option_chain(self, underlying: str) -> List[OptionChain]:
        self._require_connection()
        return list(self._chains.get(underlying, []))

    async def daily_history(
        self, symbol: str, kind: HistoryKind, days: int
    ) -> List[Bar]:
        self._require_connection()
        self.history_calls += 1
        bars = self._history.get((symbol.upper(), kind), [])
        return bars[-days:] if days else list(bars)

    # -- orders ---------------------------------------------------------------

    async def what_if(self, order: OrderRequest) -> MarginImpact:
        self._require_connection()
        return self.what_if_fn(order, self._account)

    async def place_order(self, order: OrderRequest) -> OrderState:
        self._check_can_trade()
        self._require_connection()
        if (
            order.order_ref in self._orders
            and not self._orders[order.order_ref].status.is_done
        ):
            raise OrderRejected(f"duplicate working order {order.order_ref}")
        quantities = {leg.quantity for leg in order.legs}
        if len(quantities) != 1:
            raise OrderRejected("fake broker supports equal-ratio combos only")
        if self._reject_next is not None:
            reason, self._reject_next = self._reject_next, None
            return self._set_order(
                OrderState(order, OrderStatus.REJECTED, reason=reason)
            )
        state = self._set_order(OrderState(order, OrderStatus.WORKING))
        if self.auto_fill:
            natural = self._natural_price(order)
            # Prices are net debits per unit: a credit is negative. An order is
            # marketable when it is willing to pay at least the natural price.
            if natural is not None and order.limit_price >= natural - 1e-9:
                state = self.fill_order(
                    order.order_ref, order.legs[0].quantity, order.limit_price
                )
        return state

    async def cancel_order(self, order_ref: str) -> OrderState:
        self._require_connection()
        state = self._orders.get(order_ref)
        if state is None:
            raise BrokerError(f"unknown order {order_ref}")
        if state.status.is_done:
            return state
        return self._set_order(replace(state, status=OrderStatus.CANCELLED))

    async def open_orders(self) -> List[OrderState]:
        self._require_connection()
        return [s for s in self._orders.values() if not s.status.is_done]

    async def executions(self, since: Optional[datetime] = None) -> List[Fill]:
        self._require_connection()
        return [f for f in self._fills if since is None or f.ts >= since]

    # -- internals ------------------------------------------------------------

    def _natural_price(self, order: OrderRequest) -> Optional[float]:
        total = 0.0
        for leg in order.legs:
            quote = self._quotes.get(leg.contract.key)
            if quote is None or quote.bid is None or quote.ask is None:
                return None
            total += quote.ask if leg.action is Action.BUY else -quote.bid
        return total

    def _set_order(self, state: OrderState) -> OrderState:
        self._orders[state.order.order_ref] = state
        self._publish(EVENT_ORDER, state)
        return state

    def _record_fill(
        self,
        order_ref: str,
        leg: OrderLeg,
        quantity: int,
        net_price: float,
        order: OrderRequest,
    ) -> None:
        # Per-leg price: single legs fill at the net price; combo legs at their quote.
        if len(order.legs) == 1:
            price = abs(net_price)
        else:
            quote = self._quotes.get(leg.contract.key)
            price = quote.mid if quote and quote.mid is not None else abs(net_price)
        commission = self.commission_per_contract * quantity
        fill = Fill(
            exec_id=f"fake-{next(self._exec_ids)}",
            order_ref=order_ref,
            contract=leg.contract,
            action=leg.action,
            quantity=quantity,
            price=price,
            ts=self._clock(),
            commission=commission,
        )
        self._fills.append(fill)
        signed = leg.action.sign * quantity
        self._apply_position(leg.contract, signed, price)
        self._adjust_cash(-signed * price * leg.contract.multiplier - commission)
        self._publish(EVENT_FILL, fill)

    def _apply_position(
        self,
        contract: Contract,
        quantity: float,
        price: float,
        replace_qty: bool = False,
    ) -> None:
        old = self._positions.get(contract.key)
        if replace_qty or old is None:
            new_qty, avg = quantity, price
        else:
            new_qty = old.quantity + quantity
            same_side = (old.quantity > 0) == (quantity > 0)
            if same_side and new_qty != 0:
                avg = (old.avg_cost * old.quantity + price * quantity) / new_qty
            else:
                avg = (
                    old.avg_cost
                    if new_qty and (old.quantity > 0) == (new_qty > 0)
                    else price
                )
        if new_qty == 0:
            self._positions.pop(contract.key, None)
            pos = Position(contract, 0, 0.0)
        else:
            pos = Position(contract, new_qty, avg)
            self._positions[contract.key] = pos
        self._publish(EVENT_POSITION, pos)

    def _adjust_cash(self, amount: float) -> None:
        self._account = replace(
            self._account, cash=self._account.cash + amount, ts=self._clock()
        )
