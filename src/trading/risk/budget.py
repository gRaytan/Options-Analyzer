"""Live risk budget (plan T19b, spec §6.2, §6.6).

Each engine cycle measures how much of the risk budget the **current**
options book uses: its loss in the budget scenario (-20%, IV +50%) against
10% of NLV x the regime multiplier. The VIX regime is refreshed every
5 minutes. The result goes to the heartbeat (status page) and, when it
changes meaningfully, to the journal's ``budget_snapshot`` table.

**Options-book drawdown** (spec §6.4): the options book's P&L since the
bot started (every option fill's cash flow, fees included, plus the current
options at their mid) is tracked against its peak. The drop from the peak,
as a share of NLV, halves the budget at ``options_dd_reduce`` (5%), stops new
risk at ``options_dd_halt`` (8%), and returns to normal under
``options_dd_recover`` (2%). The peak survives restarts (journaled with the
budget snapshot).

Limitation: an option that settles at expiry produces no fill, so its
settlement isn't counted; the guards close or roll anything in the money
before expiration day ends, which keeps this small.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from src.trading.broker.base import BrokerAdapter, BrokerError
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    HistoryKind,
    OptionContract,
    Position,
    utcnow,
)
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from
from src.trading.risk.regime import Regime, budget_state, classify_regime, vix_inputs
from src.trading.risk.stress import Scenario, StressModel, betas_from, crash_scenario

log = logging.getLogger(__name__)

REGIME_REFRESH = timedelta(minutes=5)
JOURNAL_EVERY = timedelta(minutes=15)


class BudgetTracker:
    def __init__(self, config: TradingConfig, journal: Journal, clock=utcnow):
        self.config = config
        self.journal = journal
        self._clock = clock
        self.regime: Optional[Regime] = None
        self._regime_at: Optional[datetime] = None
        self.reduced = False
        self.peak: Optional[float] = None  # options-book P&L peak ($)
        self.last: Optional[dict] = None
        self._journaled_at: Optional[datetime] = None

    async def _refresh_regime(self, broker: BrokerAdapter) -> Regime:
        now = self._clock()
        if (
            self.regime is not None
            and self._regime_at
            and now - self._regime_at < REGIME_REFRESH
        ):
            return self.regime
        try:
            bars = await broker.daily_history("VIX", HistoryKind.PRICE, 10)
            vix, recent = vix_inputs([b.close for b in bars])
            self.regime = classify_regime(vix, recent, self.config.regime)
        except (BrokerError, NotImplementedError) as exc:
            self.regime = Regime(
                "unknown", 0.0, detail=f"no VIX data ({exc}): no new risk"
            )
        self._regime_at = now
        return self.regime

    def _options_pnl(self, positions: List[Position], quotes) -> float:
        """Options-book P&L since the start: fill cash flows + open options at mid."""
        total = 0.0
        for rec in self.journal.read("fills"):
            f = rec.data
            c = f.contract
            if isinstance(c, OptionContract):
                total += -f.action.sign * f.price * f.quantity * c.multiplier
                total -= f.commission
        for p in positions:
            c = p.contract
            if isinstance(c, OptionContract):
                q = quotes.get(c.key)
                mark = q.mid if q is not None and q.mid is not None else p.market_price
                if mark is not None:
                    total += p.quantity * c.multiplier * mark
        return float(total)

    def _restored_peak(self) -> float:
        rec = self.journal.latest("budget_snapshot")
        return float((rec.data or {}).get("options_peak") or 0.0) if rec else 0.0

    async def update(
        self,
        broker: BrokerAdapter,
        positions: List[Position],
        account: Optional[AccountSnapshot],
    ) -> Optional[dict]:
        if account is None or account.net_liquidation <= 0:
            return self.last
        regime = await self._refresh_regime(broker)
        options = [
            p.contract for p in positions if isinstance(p.contract, OptionContract)
        ]
        quotes = {}
        if options:
            try:
                quotes = {q.contract.key: q for q in await broker.quotes(options)}
            except BrokerError as exc:
                log.warning(
                    "budget: option quotes unavailable (%s); unknown deltas/IVs", exc
                )
        options_pnl = self._options_pnl(positions, quotes)
        if self.peak is None:
            self.peak = self._restored_peak()
        self.peak = max(self.peak, options_pnl, 0.0)
        drawdown = max(0.0, self.peak - options_pnl) / account.net_liquidation
        state = budget_state(regime, drawdown, self.reduced, self.config.risk)
        self.reduced = state.reduced
        exposures = exposures_from(positions, quotes)
        model = StressModel(
            exposures, quotes, account, self._clock().date(), betas_from(self.config)
        )
        mm = self.config.risk.stress_margin_multiplier
        used = float(
            max(0.0, -model.run(Scenario("budget", -0.20, 0.50, mm)).options_pnl)
        )
        # The whole account in the crash it must survive (S&P -30% by default).
        crash = model.run(crash_scenario(self.config))
        floor = self.config.margin_floor.excess_liquidity_min
        budget = float(state.budget(account.net_liquidation, self.config.risk))
        result = {
            "regime": regime.name,
            "vix": regime.vix,
            "regime_detail": regime.detail,
            "multiplier": state.multiplier,
            "spreads_only": regime.spreads_only,
            "budget": budget,
            "used": used,
            "free": max(0.0, budget - used),
            "over_budget": bool(used > budget),
            "drawdown": drawdown,
            "options_pnl": options_pnl,
            "options_peak": self.peak,
            "reduced": state.reduced,
            "halted": state.halted,
            "crash": {
                "move": self.config.risk.crash_move,
                "stock_pnl": float(crash.stock_pnl),
                "options_pnl": float(crash.options_pnl),
                "nlv_after": float(account.net_liquidation + crash.total_pnl),
                "excess_liquidity_after": float(crash.excess_liquidity_after),
                "survives": bool(crash.excess_liquidity_after >= floor),
            },
        }
        self._maybe_journal(result, account)
        self.last = result
        return result

    def _maybe_journal(self, result: dict, account: AccountSnapshot) -> None:
        now = self._clock()
        prev = self.last
        changed = (
            prev is None
            or prev["regime"] != result["regime"]
            or prev["multiplier"] != result["multiplier"]
            or prev.get("options_peak") != result.get("options_peak")
            or abs(prev["used"] - result["used"]) > 0.01 * account.net_liquidation
            or self._journaled_at is None
            or now - self._journaled_at >= JOURNAL_EVERY
        )
        if changed:
            self.journal.append(
                "budget_snapshot", result, key=account.account_id, ts=now
            )
            self._journaled_at = now
