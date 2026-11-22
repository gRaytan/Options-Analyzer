"""Backtester: replay history through the real engine (plan T37, spec §11).

Each trading day of the window:

1. The clock is set to 20 minutes before that day's close (inside the
   order window), through :func:`src.trading.domain.set_clock`.
2. Options that expired since the last day are settled at the expiry-day
   close (index options in cash; ITM equity options become shares).
3. Margin interest on a debit balance and dividends on the shares accrue.
4. The fake broker gets that day's data, never anything later: price and IV
   history up to the day, VIX, the option chains, and a synthetic market
   (T36) quoting every option at that day's close.
5. Positions are marked to market and the account values (NLV, Reg-T
   maintenance margin, Excess Liquidity, Cushion) are computed.
6. **The real engine runs one cycle** (``run.monitor(once=True)``): margin
   monitor and restoration, guard and rolls, position manager, budget,
   scanner, entries, risk gate and order manager. Nothing is reimplemented.
7. The day's closing state is recorded.

Fills are pessimistic: the synthetic bid-ask is wide (5% of mid, >= $0.05)
and a resting order fills only once it is ``fill_threshold`` (50%) of the
way from mid to the natural price. Fees are IB's (fees.py via the fake
broker's $0.65/contract) and margin interest is charged on any debit.

A **baseline** (the same starting shares, no options, dividends only) runs
alongside, so the report shows what the options added or cost.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.trading import run
from src.trading.backtest.synthetic import SyntheticMarket
from src.trading.broker.base import MarginImpact
from src.trading.broker.fake import FakeAdapter
from src.trading.calendars import MarketCalendar
from src.trading.config import Mode, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    HistoryKind,
    OptionContract,
    OrderRequest,
    Position,
    StockContract,
    set_clock,
)
from src.trading.journal import Journal
from src.trading.notifier import Notifier
from src.trading.risk.limits import Exposure
from src.trading.risk.stress import STOCK_MAINT, _reg_t_model_margin

log = logging.getLogger("trading.backtest")

HISTORY_DAYS = 400  # what the engine sees of the past (IV rank needs a year)
BEFORE_CLOSE = timedelta(minutes=20)


@dataclass
class BacktestSettings:
    start: date
    end: date
    nlv: float = 1_000_000.0
    holdings: Dict[str, float] = field(default_factory=lambda: {"SPY": 0.5, "QQQ": 0.5})
    margin_rate: float = 0.06  # on a debit balance (IB benchmark + spread)
    fill_threshold: float = 0.5
    account_id: str = "DU-BACKTEST"


@dataclass
class DayRecord:
    day: date
    nlv: float
    excess_liquidity: float
    cushion: float
    maint_margin: float
    cash: float
    stock_value: float
    options_value: float
    baseline_nlv: float
    open_options: int


@dataclass
class BacktestResult:
    settings: BacktestSettings
    days: List[DayRecord]
    journal_path: Path
    settlements: List[tuple]  # (day, key, kind, amount)
    fallbacks: List[str]


async def _no_sleep(_seconds: float) -> None:
    return None


class Backtester:
    def __init__(
        self,
        market: SyntheticMarket,
        config: TradingConfig,
        settings: BacktestSettings,
        workdir: Optional[Path] = None,
    ):
        self.market = market
        self.config = config.model_copy(update={"mode": Mode.PAPER})
        self.s = settings
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="backtest-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.workdir / "journal.db"
        self.calendar = MarketCalendar()
        self._now = datetime.combine(settings.start, datetime.min.time(), timezone.utc)
        self.broker = FakeAdapter(
            account_id=settings.account_id,
            read_only=False,
            what_if_fn=self._what_if,
            clock=lambda: self._now,
        )
        self.broker.fill_threshold = settings.fill_threshold
        self.broker.set_quote_source(self.market.quote)
        self._atm30: Dict[str, Dict[date, float]] = {}
        self.records: List[DayRecord] = []
        self.settlements: List[tuple] = []
        self._baseline_shares: Dict[str, float] = {}
        self._baseline_cash = 0.0
        self._last_day: Optional[date] = None

    # -- prices ------------------------------------------------------------------

    def _close_on_or_before(self, symbol: str, day: date) -> Optional[float]:
        for k in range(8):
            px = self.market.price(symbol, day - timedelta(days=k))
            if px is not None:
                return px
        return None

    def _price_of(self, contract) -> Optional[float]:
        if isinstance(contract, StockContract):
            return self.market.price(contract.symbol, self.market.day)
        q = self.market.quote(contract, self.market.day)
        return q.mid if q is not None else None

    # -- account ----------------------------------------------------------------

    def _margin(self, positions: List[Position]) -> tuple:
        """(maintenance, initial) Reg-T margin for these positions at today's marks."""
        exposures, prices, values = [], {}, {}
        stock_value = 0.0
        for p in positions:
            c = p.contract
            und = c.symbol if isinstance(c, StockContract) else c.underlying
            px = self._close_on_or_before(und, self.market.day) or 0.0
            prices[und] = px
            if isinstance(c, OptionContract):
                values[c.key] = p.market_price or 0.0
            elif p.quantity > 0:
                stock_value += p.quantity * px
            exposures.append(Exposure(c, p.quantity, 0.0, px))
        maint = _reg_t_model_margin(exposures, prices, values)
        return maint, maint + (0.50 - STOCK_MAINT) * stock_value

    def _snapshot(self) -> AccountSnapshot:
        positions = list(
            self.broker._positions.values()
        )  # pylint: disable=protected-access
        cash = self.broker._account.cash  # pylint: disable=protected-access
        value = sum(
            p.quantity * (p.market_price or 0.0) * p.contract.multiplier
            for p in positions
        )
        nlv = cash + value
        maint, init = self._margin(positions)
        return AccountSnapshot(
            self.s.account_id,
            self._now,
            nlv,
            nlv - maint,
            maint,
            init,
            nlv - init,
            cash,
        )

    def _what_if(self, order: OrderRequest, account: AccountSnapshot) -> MarginImpact:
        """Margin after the order, from the same Reg-T model (no IB here)."""
        after = {
            k: p
            for k, p in self.broker._positions.items()  # pylint: disable=protected-access
        }
        for leg in order.legs:
            c = leg.contract
            old = after.get(c.key)
            qty = (old.quantity if old else 0.0) + leg.action.sign * leg.quantity
            after[c.key] = Position(c, qty, 0.0, self._price_of(c) or 0.0)
        maint, init = self._margin([p for p in after.values() if p.quantity])
        equity = account.net_liquidation
        return MarginImpact(equity, init, maint)

    def _refresh_account(self) -> AccountSnapshot:
        self.broker.mark(self._price_of)
        snap = self._snapshot()
        self.broker._account = snap  # pylint: disable=protected-access
        return snap

    # -- data the engine sees (no look-ahead) -------------------------------------------

    def _atm_series(self, symbol: str) -> Dict[date, float]:
        if symbol not in self._atm30:
            self._atm30[symbol] = {
                d: v
                for d in self.market.history.days(symbol)
                if (v := self.market.atm_iv(symbol, d, 30)) is not None
            }
        return self._atm30[symbol]

    def _feed(self, day: date) -> None:
        since = day - timedelta(days=HISTORY_DAYS)
        symbols = {
            self.config.entry.index_scan_root,
            *self.s.holdings,
            *self.config.watchlist,
        }
        for sym in sorted(symbols):
            bars = [
                b
                for b in self.market.history.bars.get(sym.upper(), [])
                if since <= b.day <= day
            ]
            if not bars:
                continue
            self.broker.set_history(sym, HistoryKind.PRICE, bars)
            ivs = self._atm_series(sym.upper())
            self.broker.set_history(
                sym,
                HistoryKind.IMPLIED_VOL,
                [
                    Bar(d, v, v, v, v)
                    for d, v in sorted(ivs.items())
                    if since <= d <= day
                ],
            )
            self.broker.set_chain(sym, self.market.chain(sym, day))
        vix = self.market.history.vol.get("VIX", {})
        self.broker.set_history(
            "VIX",
            HistoryKind.PRICE,
            [
                Bar(d, v, v, v, v)
                for d, v in sorted(vix.items())
                if day - timedelta(days=20) <= d <= day
            ],
        )

    # -- the daily loop ---------------------------------------------------------------

    def _trading_days(self) -> List[date]:
        days = set(self.market.history.days(self.config.entry.index_scan_root))
        for sym in self.s.holdings:
            days &= set(self.market.history.days(sym))
        days &= set(self.market.history.vol.get("VIX", {}))
        return sorted(d for d in days if self.s.start <= d <= self.s.end)

    def _start(self, day: date) -> None:
        cash = self.s.nlv
        for sym, weight in self.s.holdings.items():
            px = self.market.price(sym, day)
            shares = math.floor(self.s.nlv * weight / px)
            self.broker.set_position(StockContract(sym), shares, px)
            self._baseline_shares[sym] = shares
            cash -= shares * px
        self.broker.set_account(cash=cash)
        self._baseline_cash = cash

    def _carry(self, day: date) -> None:
        """Interest on a debit and dividends since the last day (both sides)."""
        if self._last_day is None:
            return
        n = (day - self._last_day).days
        acct = self.broker._account  # pylint: disable=protected-access
        interest = -acct.cash * self.s.margin_rate * n / 360 if acct.cash < 0 else 0.0
        divs = 0.0
        for p in self.broker._positions.values():  # pylint: disable=protected-access
            if isinstance(p.contract, StockContract) and p.quantity > 0:
                q = self.market.p.dividend_yield.get(p.contract.symbol, 0.0)
                px = self._close_on_or_before(p.contract.symbol, self._last_day) or 0.0
                divs += p.quantity * px * q * n / 365
        self.broker._adjust_cash(divs - interest)  # pylint: disable=protected-access
        for sym, shares in self._baseline_shares.items():
            q = self.market.p.dividend_yield.get(sym, 0.0)
            px = self._close_on_or_before(sym, self._last_day) or 0.0
            self._baseline_cash += shares * px * q * n / 365

    def _settle(self, day: date) -> None:
        def close_on(und, expiry):
            return self._close_on_or_before(und, expiry)

        for key, kind, amount in self.broker.settle_expired(day, close_on):
            self.settlements.append((day, key, kind, amount))

    def _record(self, day: date) -> None:
        snap = self._refresh_account()
        stock = opts = 0.0
        n_opts = 0
        for p in self.broker._positions.values():  # pylint: disable=protected-access
            v = p.quantity * (p.market_price or 0.0) * p.contract.multiplier
            if isinstance(p.contract, OptionContract):
                opts += v
                n_opts += 1
            else:
                stock += v
        baseline = self._baseline_cash + sum(
            n * (self.market.price(s, day) or 0.0)
            for s, n in self._baseline_shares.items()
        )
        self.records.append(
            DayRecord(
                day,
                snap.net_liquidation,
                snap.excess_liquidity,
                snap.excess_liquidity / snap.net_liquidation
                if snap.net_liquidation
                else 0.0,
                snap.maint_margin,
                snap.cash,
                stock,
                opts,
                baseline,
                n_opts,
            )
        )

    async def _one_day(self, day: date) -> None:
        session = self.calendar.session(day)
        if session is None:
            return
        self._now = session[1] - BEFORE_CLOSE
        set_clock(lambda: self._now)
        self.market.day = day
        if not self.records and self._last_day is None:
            self._start(day)
        self._settle(day)
        self._carry(day)
        self._feed(day)
        self._refresh_account()
        await run.monitor(
            0,
            once=True,
            adapter=self.broker,
            journal=Journal(self.journal_path),
            halt_file=self.workdir / "HALT",
            notifier=Notifier(self.config.alerts),
            config=self.config,
            order_sleep=_no_sleep,
        )
        self._record(day)
        self._last_day = day

    async def run(self, progress=None) -> BacktestResult:
        old_env = {k: os.environ.get(k) for k in ("TRADING_CACHE_DIR",)}
        old_hb = run.HEARTBEAT_FILE
        os.environ["TRADING_CACHE_DIR"] = str(self.workdir / "cache")
        run.HEARTBEAT_FILE = str(self.workdir / "heartbeat.json")
        try:
            days = self._trading_days()
            for i, day in enumerate(days):
                await self._one_day(day)
                if progress is not None:
                    progress(
                        i + 1, len(days), self.records[-1] if self.records else None
                    )
        finally:
            set_clock(None)
            run.HEARTBEAT_FILE = old_hb
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return BacktestResult(
            self.s,
            self.records,
            self.journal_path,
            self.settlements,
            sorted(self.market.fallbacks),
        )


def run_backtest(
    market, config, settings, workdir=None, progress=None
) -> BacktestResult:
    return asyncio.run(Backtester(market, config, settings, workdir).run(progress))
