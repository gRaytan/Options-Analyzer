"""Backtest report (plan T37): returns, drawdown, options P&L, fees, floor breaches.

**Trades** are found from the journal's fills: option keys traded in the
same order (a spread's two legs, a roll's old and new leg) belong to the
same trade, so a put that was rolled twice is one trade. A trade is closed
when all its options are flat; its P&L is every premium in and out, fees,
and the settlement at expiry. Win rate counts closed trades with P&L > 0.

**Options contribution** = the account's NLV minus the baseline's (the
same starting shares, no options): what selling options added (or cost),
after fees and margin interest.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Dict, List, Optional

from src.trading.backtest.engine import BacktestResult, DayRecord
from src.trading.domain import OptionContract
from src.trading.journal import Journal

FLOOR = 100_000.0


def _max_drawdown(values: List[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return -worst


def _cagr(start: float, end: float, days: int) -> float:
    if start <= 0 or end <= 0 or days <= 0:
        return 0.0
    return (end / start) ** (365.25 / days) - 1


@dataclass
class Report:
    start: str
    end: str
    trading_days: int
    start_nlv: float
    end_nlv: float
    total_return: float
    cagr: float
    max_drawdown: float
    baseline_end_nlv: float
    baseline_return: float
    baseline_max_drawdown: float
    options_contribution: float  # $ (NLV - baseline at the end)
    options_contribution_pct: float  # of the starting NLV, per year
    worst_options_day: float  # $ (NLV change - baseline change)
    premium_sold: float
    premium_bought: float
    fees: float
    fees_pct_of_premium: float
    margin_interest_note: str
    trades: int
    closed_trades: int
    wins: int
    win_rate: float
    avg_win: float
    avg_loss: float
    biggest_loss: float
    trades_by_strategy: Dict[str, int] = field(default_factory=dict)
    exits: Dict[str, int] = field(default_factory=dict)
    settlements: Dict[str, int] = field(default_factory=dict)
    min_excess_liquidity: float = 0.0
    min_cushion: float = 0.0
    days_below_floor: int = 0
    margin_states: Dict[str, int] = field(default_factory=dict)
    gate_rejections: Dict[str, int] = field(default_factory=dict)
    halts: List[str] = field(default_factory=list)
    data_notes: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)


class _Trades:
    """Union-find over option keys that shared an order."""

    def __init__(self):
        self.parent: Dict[str, str] = {}

    def find(self, k: str) -> str:
        self.parent.setdefault(k, k)
        while self.parent[k] != k:
            self.parent[k] = self.parent[self.parent[k]]
            k = self.parent[k]
        return k

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def build_report(result: BacktestResult) -> Report:
    days: List[DayRecord] = result.days
    if not days:
        raise ValueError("the backtest produced no days (check the data window)")
    j = Journal(result.journal_path)
    try:
        fills = [r.data for r in j.read("fills")]
        intents = {}
        for r in j.read("intents"):
            intents[r.key] = r.data
        risk = [r.data for r in j.read("risk_checks")]
        margin_events = [r.data for r in j.read("margin_events")]
        halts = [r.data for r in j.read("halts")]
    finally:
        j.close()

    # -- trades ------------------------------------------------------------------
    uf = _Trades()
    by_order: Dict[str, List[str]] = defaultdict(list)
    flows: Dict[str, float] = defaultdict(float)
    qty: Dict[str, float] = defaultdict(float)
    sold = bought = fees = 0.0
    opened_by: Dict[str, str] = {}
    for f in fills:
        c = f.contract
        if not isinstance(c, OptionContract):
            fees += f.commission
            continue
        k = c.key
        uf.find(k)
        by_order[f.order_ref].append(k)
        amount = f.price * f.quantity * c.multiplier
        if f.action.sign < 0:
            sold += amount
        else:
            bought += amount
        flows[k] += -f.action.sign * amount - f.commission
        qty[k] += f.action.sign * f.quantity
        fees += f.commission
        it = intents.get(f.order_ref)
        if it is not None and it.trigger == "SCAN_READY":
            opened_by.setdefault(k, it.strategy)
    for keys in by_order.values():
        for k in keys[1:]:
            uf.union(keys[0], k)
    settled = Counter()
    for _day, key, kind, amount in result.settlements:
        settled[kind] += 1
        # Cash settlement pays the intrinsic value; a physical assignment moves
        # it into shares (NLV has it): either way the trade's P&L counts it.
        flows[key] += amount
        qty[key] = 0.0  # gone at expiry
        uf.find(key)
    groups: Dict[str, List[str]] = defaultdict(list)
    for k in list(uf.parent):
        groups[uf.find(k)].append(k)
    trade_pnl = []
    strategies = Counter()
    for keys in groups.values():
        strat = next((opened_by[k] for k in keys if k in opened_by), "other")
        strategies[strat] += 1
        if all(abs(qty[k]) < 1e-9 for k in keys):
            trade_pnl.append(sum(flows[k] for k in keys))
    wins = [p for p in trade_pnl if p > 0]
    losses = [p for p in trade_pnl if p <= 0]

    # -- account ---------------------------------------------------------------------
    first, last = days[0], days[-1]
    n_days = (last.day - first.day).days
    contribution = last.nlv - last.baseline_nlv
    worst_day = 0.0
    for a, b in zip(days, days[1:]):
        worst_day = min(worst_day, (b.nlv - a.nlv) - (b.baseline_nlv - a.baseline_nlv))
    exits = Counter(
        it.trigger for it in intents.values() if it.kind.value in ("close", "roll")
    )
    rejections = Counter()
    for r in risk:
        if not r.get("approved"):
            for c in r.get("checks", []):
                if not c.get("passed"):
                    rejections[c.get("name", "?")] += 1
    years = max(n_days / 365.25, 1 / 365.25)
    return Report(
        start=first.day.isoformat(),
        end=last.day.isoformat(),
        trading_days=len(days),
        start_nlv=first.nlv,
        end_nlv=last.nlv,
        total_return=last.nlv / first.nlv - 1,
        cagr=_cagr(first.nlv, last.nlv, n_days),
        max_drawdown=_max_drawdown([d.nlv for d in days]),
        baseline_end_nlv=last.baseline_nlv,
        baseline_return=last.baseline_nlv / first.baseline_nlv - 1,
        baseline_max_drawdown=_max_drawdown([d.baseline_nlv for d in days]),
        options_contribution=contribution,
        options_contribution_pct=contribution / first.nlv / years,
        worst_options_day=worst_day,
        premium_sold=sold,
        premium_bought=bought,
        fees=fees,
        fees_pct_of_premium=fees / sold if sold else 0.0,
        margin_interest_note=f"charged at {result.settings.margin_rate:.1%} on any debit",
        trades=len(groups),
        closed_trades=len(trade_pnl),
        wins=len(wins),
        win_rate=len(wins) / len(trade_pnl) if trade_pnl else 0.0,
        avg_win=sum(wins) / len(wins) if wins else 0.0,
        avg_loss=sum(losses) / len(losses) if losses else 0.0,
        biggest_loss=min(trade_pnl, default=0.0),
        trades_by_strategy=dict(strategies),
        exits=dict(exits),
        settlements=dict(settled),
        min_excess_liquidity=min(d.excess_liquidity for d in days),
        min_cushion=min(d.cushion for d in days),
        days_below_floor=sum(1 for d in days if d.excess_liquidity < FLOOR),
        margin_states=dict(Counter(e.get("state") for e in margin_events)),
        gate_rejections=dict(rejections),
        halts=[
            getattr(h, "reason", str(h)) for h in halts if getattr(h, "active", False)
        ],
        data_notes=list(result.fallbacks),
    )


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def to_markdown(r: Report, title: Optional[str] = None) -> str:
    lines = [
        f"# {title or 'Backtest'}: {r.start} to {r.end}",
        "",
        "| | Bot (shares + options) | Shares only |",
        "|---|---|---|",
        f"| End NLV | ${r.end_nlv:,.0f} | ${r.baseline_end_nlv:,.0f} |",
        f"| Total return | {_pct(r.total_return)} | {_pct(r.baseline_return)} |",
        f"| CAGR | {_pct(r.cagr)} | |",
        f"| Max drawdown | {r.max_drawdown:.1%} | {r.baseline_max_drawdown:.1%} |",
        "",
        f"**Options contribution:** ${r.options_contribution:,.0f} "
        f"({_pct(r.options_contribution_pct)} of starting NLV per year), "
        f"after fees and margin interest. Worst day for the options: ${r.worst_options_day:,.0f}.",
        "",
        "## Trades",
        "",
        f"- {r.trades} trades ({', '.join(f'{k}: {v}' for k, v in sorted(r.trades_by_strategy.items())) or 'none'}), "
        f"{r.closed_trades} closed.",
        f"- Win rate **{r.win_rate:.0%}** ({r.wins}/{r.closed_trades}); average win "
        f"${r.avg_win:,.0f}, average loss ${r.avg_loss:,.0f}, biggest loss ${r.biggest_loss:,.0f}.",
        f"- Premium sold ${r.premium_sold:,.0f}, bought back ${r.premium_bought:,.0f}.",
        f"- Fees ${r.fees:,.0f} = **{r.fees_pct_of_premium:.1%}** of premium sold; "
        f"margin interest {r.margin_interest_note}.",
        f"- Exits: {', '.join(f'{k} {v}' for k, v in sorted(r.exits.items())) or 'none'}. "
        f"Expiries: {', '.join(f'{k} {v}' for k, v in sorted(r.settlements.items())) or 'none'}.",
        "",
        "## Margin safety (R1, R9)",
        "",
        f"- Lowest Excess Liquidity ${r.min_excess_liquidity:,.0f}; lowest Cushion {r.min_cushion:.0%}.",
        f"- Days below the $100k floor: **{r.days_below_floor}**.",
        f"- Margin states (logged each day): {', '.join(f'{k} {v}' for k, v in sorted(r.margin_states.items()))}.",
        f"- Gate rejections: {', '.join(f'{k} {v}' for k, v in sorted(r.gate_rejections.items())) or 'none'}.",
        f"- Engine halts: {len(r.halts)}"
        + (f" ({'; '.join(r.halts[:3])})" if r.halts else "")
        + ".",
    ]
    if r.data_notes:
        lines += ["", "## Data notes", ""] + [f"- {n}" for n in r.data_notes]
    lines += [
        "",
        "_Synthetic option prices (spec §11.1): conservative skew, 5% bid-ask, "
        "fills halfway from mid to the natural price._",
    ]
    return "\n".join(lines) + "\n"
