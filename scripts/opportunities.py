#!/usr/bin/env python3
"""Today's option-selling opportunities from your IB account (read-only).

Connects to IB Gateway read-only, reads your positions and live market data,
runs the bot's scanner (covered calls on your holdings, XSP puts and put
spreads, plus any single-stock puts on the watchlist) and prints the best
candidates. Results also go to the journal, so the web UI's Opportunities
page shows them. Nothing is ever ordered.

    python scripts/opportunities.py --account DU1234567          # paper, port 4002
    python scripts/opportunities.py --account U1234567 --port 4001

When the always-on engine runs (deploy/README.md), it does this every
15 minutes in market hours by itself and sends a digest at 10:00 ET; this
script is for running it on your own computer until then, e.g. every
weekday at 10:00 ET from cron or Windows Task Scheduler.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading.broker.base import BrokerError  # noqa: E402
from src.trading.broker.ib_adapter import IBAdapter, IBConnectionSettings  # noqa: E402
from src.trading.calendars import EventCalendar  # noqa: E402
from src.trading.config import load_trading_config  # noqa: E402
from src.trading.domain import utcnow  # noqa: E402
from src.trading.history import HistoryStore  # noqa: E402
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal  # noqa: E402
from src.trading.risk.budget import BudgetTracker  # noqa: E402
from src.trading.scanner import report_strategies, scan  # noqa: E402
from src.trading.scheduler import ET  # noqa: E402
from src.trading.tracker import Tracker  # noqa: E402


def table(result, n: int) -> str:
    rows = [
        f"{'':6s}{'strategy':13s}{'contract':32s}{'qty':>4s}{'delta':>7s}"
        f"{'bid/ask':>14s}{'credit':>9s}{'EV':>8s}{'yield/yr':>9s}{'timing':>8s}"
    ]
    for c in result.best(n):
        q = c.quote
        key = c.contract.key + (f" / {c.long_contract.strike:g}" if c.is_spread else "")
        rows.append(
            f"{'READY' if c.fire else 'wait':6s}{c.strategy.value:13s}{key:32s}"
            f"{c.contracts:>4d}{abs(q.greeks.delta) if q.greeks else 0:>7.2f}"
            f"{q.bid or 0:>7.2f}/{q.ask or 0:<6.2f}{c.net_credit:>9,.0f}{c.ev:>8,.0f}"
            f"{c.annual_yield:>9.0%}{c.timing:>8.2f}"
        )
    return "\n".join(rows)


async def main(args) -> int:
    config = load_trading_config()
    settings = IBConnectionSettings(
        host=args.host, port=args.port, client_id=args.client_id
    )
    adapter = IBAdapter(args.account, settings, read_only=True)
    journal = Journal(os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH)))
    try:
        await adapter.connect()
    except BrokerError as exc:
        print(f"Cannot connect to IB: {exc}. Is IB Gateway running and logged in?")
        return 1
    try:
        snap = await adapter.account_snapshot()
        positions = await adapter.positions()
        budget = await BudgetTracker(config, journal).update(adapter, positions, snap)
        regime = (budget or {}).get("regime", "unknown")
        today = utcnow().astimezone(ET).date()
        print(
            f"{args.account}: NLV ${snap.net_liquidation:,.0f}, Excess Liquidity "
            f"${snap.excess_liquidity:,.0f}, Cushion {snap.cushion:.0%}"
        )
        if budget:
            print(
                f"VIX regime: {regime} ({budget.get('regime_detail', '')}); risk budget "
                f"${budget['used']:,.0f} used of ${budget['budget']:,.0f}"
            )
        result = await scan(
            adapter,
            HistoryStore(adapter),
            positions,
            snap,
            config,
            today,
            events=EventCalendar(),
            journal=journal,
            tracker=Tracker(journal),
            regime=regime,
            strategies=report_strategies(config),
        )
    finally:
        await adapter.disconnect()
        journal.close()
    if result.candidates:
        print(
            f"\n{len(result.candidates)} candidates, {len(result.ready())} ready to sell "
            "(READY = the timing is good now):\n"
        )
        print(table(result, args.top))
    else:
        print("\nNothing passes the filters today.")
    if result.rejected:
        top = sorted(result.rejected.items(), key=lambda kv: -kv[1])[:5]
        print(
            "\nMost common reasons options were skipped: "
            + "; ".join(f"{k} ({v})" for k, v in top)
        )
    print(
        "\nNothing was ordered. The bot's risk gate checks margin and the crash test "
        "before any trade; approve trades on the Approvals or Rebuild pages."
    )
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--account", default=os.environ.get("TRADING_ACCOUNT_ID"))
    p.add_argument("--host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("IB_PORT", 4002)))
    p.add_argument(
        "--client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", 6))
    )
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args()
    if not a.account:
        p.error("--account is required (or set TRADING_ACCOUNT_ID)")
    sys.exit(asyncio.run(main(a)))
