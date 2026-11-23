"""Bot metrics from the journal (plan T40, spec §9).

Everything is computed from what the engine journals (fills, intents,
orders, account snapshots), so the Bot metrics page, the backtest report
and the yearly tax export agree.

**Trades.** Option fills from the same order belong together (a spread's
two legs, a roll's old and new leg), so a put rolled twice is **one trade**
(a roll chain). A trade is *closed* once all its options are flat or
expired; its realized P&L is all premium in and out, minus fees, plus any
settlement at expiry.

Live journals don't record expiry settlements: an option that disappears
at expiry is assumed to have **expired worthless** (the guards close or roll
anything in the money before expiration day ends, spec §5.11). Backtests
pass their exact settlements.

**Slippage** compares each order's fill with its first limit price, which is
the mid when the order was placed. Positive = paid away vs the mid.
"""

from __future__ import annotations

import csv
import io
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.trading.domain import IntentKind, OptionContract

UNKNOWN = "other"


@dataclass
class Trade:
    trade_id: str
    strategy: str
    keys: List[str]
    opened: Optional[datetime] = None
    closed: Optional[date] = None  # None = still open
    pnl: float = 0.0  # realized (or so far, for open trades: premium net of buys)
    premium_sold: float = 0.0
    fees: float = 0.0
    orders: int = 0
    rolls: int = 0
    expired_worthless: List[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.closed is not None


class _UnionFind:
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


def trades_from(
    fills: Sequence,
    intents: Dict[str, object],
    settlements: Iterable[Tuple] = (),
    today: Optional[date] = None,
) -> List[Trade]:
    """Group option fills into trades. ``settlements``: (day, key, kind, $ amount)."""
    uf = _UnionFind()
    contracts: Dict[str, OptionContract] = {}
    by_order: Dict[str, List[str]] = defaultdict(list)
    flows: Dict[str, float] = defaultdict(float)
    qty: Dict[str, float] = defaultdict(float)
    sold: Dict[str, float] = defaultdict(float)
    fees: Dict[str, float] = defaultdict(float)
    first_ts: Dict[str, datetime] = {}
    last_ts: Dict[str, date] = {}
    for f in fills:
        c = f.contract
        if not isinstance(c, OptionContract):
            continue
        k = c.key
        uf.find(k)
        contracts[k] = c
        if k not in by_order[f.order_ref]:
            by_order[f.order_ref].append(k)
        amount = f.price * f.quantity * c.multiplier
        if f.action.sign < 0:
            sold[k] += amount
        flows[k] += -f.action.sign * amount - f.commission
        fees[k] += f.commission
        qty[k] += f.action.sign * f.quantity
        first_ts.setdefault(k, f.ts)
        last_ts[k] = f.ts.date()
    for keys in by_order.values():
        for k in keys[1:]:
            uf.union(keys[0], k)
    settled: Dict[str, date] = {}
    for day, key, _kind, amount in settlements:
        uf.find(key)
        flows[key] += amount
        qty[key] = 0.0
        settled[key] = day

    groups: Dict[str, List[str]] = defaultdict(list)
    for k in list(uf.parent):
        groups[uf.find(k)].append(k)
    order_groups: Dict[str, List[str]] = defaultdict(list)  # group root -> order refs
    for ref, keys in by_order.items():
        order_groups[uf.find(keys[0])].append(ref)

    out = []
    for root, keys in groups.items():
        refs = sorted(
            order_groups.get(root, []),
            key=lambda r: min(first_ts.get(k, datetime.max) for k in by_order[r]),
        )
        strategy = UNKNOWN
        rolls = 0
        for ref in refs:
            it = intents.get(ref)
            if it is None:
                continue
            if strategy == UNKNOWN and it.kind is IntentKind.OPEN:
                strategy = it.strategy
            if it.kind is IntentKind.ROLL:
                rolls += 1
        if strategy == UNKNOWN and refs and intents.get(refs[0]) is not None:
            strategy = intents[refs[0]].strategy
        worthless = []
        closed_on: List[date] = []
        all_flat = True
        for k in keys:
            if abs(qty[k]) < 1e-9:
                closed_on.append(settled.get(k) or last_ts.get(k) or today or date.min)
            elif today is not None and k in contracts and contracts[k].expiry < today:
                worthless.append(k)  # live journal: vanished at expiry
                closed_on.append(contracts[k].expiry)
            else:
                all_flat = False
        opened = min((first_ts[k] for k in keys if k in first_ts), default=None)
        out.append(
            Trade(
                trade_id=root,
                strategy=strategy,
                keys=sorted(keys),
                opened=opened,
                closed=max(closed_on) if all_flat and closed_on else None,
                pnl=sum(flows[k] for k in keys),
                premium_sold=sum(sold[k] for k in keys),
                fees=sum(fees[k] for k in keys),
                orders=len(refs),
                rolls=rolls,
                expired_worthless=worthless,
            )
        )
    out.sort(key=lambda t: t.opened or datetime.min)
    return out


@dataclass
class StrategyStats:
    trades: int = 0
    closed: int = 0
    wins: int = 0
    pnl: float = 0.0
    premium: float = 0.0
    fees: float = 0.0

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.closed if self.closed else None


def by_strategy(trades: Iterable[Trade]) -> Dict[str, StrategyStats]:
    out: Dict[str, StrategyStats] = defaultdict(StrategyStats)
    for t in trades:
        s = out[t.strategy]
        s.trades += 1
        s.premium += t.premium_sold
        s.fees += t.fees
        if t.is_closed:
            s.closed += 1
            s.pnl += t.pnl
            s.wins += t.pnl > 0
    return dict(out)


def slippage(fills: Sequence, orders: Sequence) -> List[dict]:
    """Per filled order: fill vs the first limit (the mid when placed), in $."""
    first_limit: Dict[str, float] = {}
    for o in orders:
        first_limit.setdefault(o.order_ref, o.limit_price)
    legs: Dict[str, Dict[str, list]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0.0, 0, None])
    )
    for f in fills:
        if not isinstance(f.contract, OptionContract) or f.order_ref not in first_limit:
            continue
        rec = legs[f.order_ref][f.contract.key]
        rec[0] += f.quantity
        rec[1] += f.action.sign * f.price * f.quantity
        rec[2] = f.contract.multiplier
        rec[3] = f.ts
    out = []
    for ref, by_key in legs.items():
        qty = min(r[0] for r in by_key.values())
        if qty <= 0:
            continue
        mult = next(iter(by_key.values()))[2]
        net = sum(r[1] for r in by_key.values()) / qty  # net debit per unit
        slip = (net - first_limit[ref]) * mult * qty
        out.append(
            {
                "order": ref,
                "ts": max(r[3] for r in by_key.values()),
                "mid": first_limit[ref],
                "filled": net,
                "contracts": qty,
                "slippage": slip,
            }
        )
    return out


def daily_nlv(snapshots: Iterable) -> List[Tuple[date, float]]:
    last: Dict[date, float] = {}
    for s in snapshots:
        last[s.ts.date()] = s.net_liquidation
    return sorted(last.items())


def drawdown(values: Sequence[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return -worst


def sortino(values: Sequence[float], periods: int = 252) -> Optional[float]:
    """Annualized Sortino ratio of daily returns (risk-free 0)."""
    rets = [b / a - 1 for a, b in zip(values, values[1:]) if a > 0]
    if len(rets) < 20:
        return None
    downside = [min(r, 0.0) for r in rets]
    dd = math.sqrt(sum(d * d for d in downside) / len(rets))
    if dd == 0:
        return None
    return sum(rets) / len(rets) / dd * math.sqrt(periods)


def monthly(trades: Iterable[Trade], fills: Sequence) -> Dict[str, Dict[str, float]]:
    """month -> premium sold, fees (by fill month) and realized P&L (by close month)."""
    out: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"premium": 0.0, "fees": 0.0, "pnl": 0.0}
    )
    for f in fills:
        if not isinstance(f.contract, OptionContract):
            continue
        m = f.ts.strftime("%Y-%m")
        out[m]["fees"] += f.commission
        if f.action.sign < 0:
            out[m]["premium"] += f.price * f.quantity * f.contract.multiplier
    for t in trades:
        if t.is_closed:
            out[t.closed.strftime("%Y-%m")]["pnl"] += t.pnl
    return dict(sorted(out.items()))


@dataclass
class BotMetrics:
    trades: List[Trade]
    strategies: Dict[str, StrategyStats]
    premium_sold: float
    premium_bought: float
    fees: float
    slippage: List[dict]
    months: Dict[str, Dict[str, float]]
    nlv: List[Tuple[date, float]]

    @property
    def closed(self) -> List[Trade]:
        return [t for t in self.trades if t.is_closed]

    @property
    def win_rate(self) -> Optional[float]:
        c = self.closed
        return sum(t.pnl > 0 for t in c) / len(c) if c else None

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    @property
    def fees_pct(self) -> Optional[float]:
        return self.fees / self.premium_sold if self.premium_sold else None

    @property
    def roll_rate(self) -> Optional[float]:
        return (
            sum(t.rolls for t in self.trades) / len(self.trades)
            if self.trades
            else None
        )

    @property
    def losing_rate(self) -> Optional[float]:
        c = self.closed
        return sum(t.pnl <= 0 for t in c) / len(c) if c else None

    @property
    def total_slippage(self) -> float:
        return sum(s["slippage"] for s in self.slippage)

    @property
    def slippage_pct(self) -> Optional[float]:
        return self.total_slippage / self.premium_sold if self.premium_sold else None

    @property
    def max_drawdown(self) -> Optional[float]:
        return drawdown([v for _, v in self.nlv]) if self.nlv else None

    @property
    def sortino(self) -> Optional[float]:
        return sortino([v for _, v in self.nlv])


def compute(
    journal,
    today: Optional[date] = None,
    settlements: Iterable[Tuple] = (),
    nlv_days: int = 400,
) -> BotMetrics:
    fills = [r.data for r in journal.read("fills")]
    intents: Dict[str, object] = {}
    for r in journal.read("intents"):
        intents[r.key] = r.data
    orders = [r.data for r in journal.read("orders")]
    snaps = []
    if fills or today is not None:
        since = datetime.combine(
            (today or date.today()) - timedelta(days=nlv_days), datetime.min.time()
        )
        from datetime import timezone  # pylint: disable=import-outside-toplevel

        snaps = [
            r.data
            for r in journal.read(
                "account_snapshot", since=since.replace(tzinfo=timezone.utc)
            )
        ]
    trades = trades_from(fills, intents, settlements, today)
    opt = [f for f in fills if isinstance(f.contract, OptionContract)]
    return BotMetrics(
        trades=trades,
        strategies=by_strategy(trades),
        premium_sold=sum(
            f.price * f.quantity * f.contract.multiplier
            for f in opt
            if f.action.sign < 0
        ),
        premium_bought=sum(
            f.price * f.quantity * f.contract.multiplier
            for f in opt
            if f.action.sign > 0
        ),
        fees=sum(f.commission for f in fills),
        slippage=slippage(fills, orders),
        months=monthly(trades, fills),
        nlv=daily_nlv(snaps),
    )


def realized_csv(trades: Iterable[Trade], year: Optional[int] = None) -> str:
    """Closed trades (per trade, USD, fees included) for tax filing (spec §9)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(
        [
            "opened",
            "closed",
            "strategy",
            "contracts",
            "premium_sold_usd",
            "fees_usd",
            "realized_pnl_usd",
            "rolls",
            "note",
        ]
    )
    for t in trades:
        if not t.is_closed or (year is not None and t.closed.year != year):
            continue
        note = (
            "expired worthless: " + " ".join(t.expired_worthless)
            if t.expired_worthless
            else ""
        )
        w.writerow(
            [
                t.opened.date().isoformat() if t.opened else "",
                t.closed.isoformat(),
                t.strategy,
                " | ".join(t.keys),
                f"{t.premium_sold:.2f}",
                f"{t.fees:.2f}",
                f"{t.pnl:.2f}",
                t.rolls,
                note,
            ]
        )
    return buf.getvalue()
