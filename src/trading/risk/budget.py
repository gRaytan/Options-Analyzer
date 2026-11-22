"""Live risk budget (plan T19b, spec §6.2, §6.6).

Each engine cycle measures how much of the risk budget the **current**
options book uses: its loss in the budget scenario (-20%, IV +50%) against
10% of NLV x the regime multiplier. The VIX regime is refreshed every
5 minutes. The result goes to the heartbeat (status page) and, when it
changes meaningfully, to the journal's ``budget_snapshot`` table.

Options-book drawdown (which halves the budget) needs options P&L history,
which arrives with metrics (T40). Until then drawdown is reported as
"not tracked" and treated as 0.
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
from src.trading.risk.stress import Scenario, StressModel

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

    async def update(
        self,
        broker: BrokerAdapter,
        positions: List[Position],
        account: Optional[AccountSnapshot],
    ) -> Optional[dict]:
        if account is None or account.net_liquidation <= 0:
            return self.last
        regime = await self._refresh_regime(broker)
        state = budget_state(regime, 0.0, self.reduced, self.config.risk)
        self.reduced = state.reduced
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
        exposures = exposures_from(positions, quotes)
        model = StressModel(exposures, quotes, account, self._clock().date())
        mm = self.config.risk.stress_margin_multiplier
        used = float(
            max(0.0, -model.run(Scenario("budget", -0.20, 0.50, mm)).options_pnl)
        )
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
            "drawdown": None,  # tracked from T40
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
            or abs(prev["used"] - result["used"]) > 0.01 * account.net_liquidation
            or self._journaled_at is None
            or now - self._journaled_at >= JOURNAL_EVERY
        )
        if changed:
            self.journal.append(
                "budget_snapshot", result, key=account.account_id, ts=now
            )
            self._journaled_at = now
