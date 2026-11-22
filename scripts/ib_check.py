#!/usr/bin/env python3
"""Check the connection to IB Gateway / TWS (read-only).

Usage (from the repository root, with IB Gateway running and logged in):

    python scripts/ib_check.py --account DU1234567            # paper, port 4002
    python scripts/ib_check.py --account U1234567 --port 4001 # live, read-only

The script never places orders: it always connects read-only.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading.broker.base import BrokerError  # noqa: E402
from src.trading.config import load_trading_config  # noqa: E402
from src.trading.domain import OptionContract, StockContract  # noqa: E402
from src.trading.broker.ib_adapter import IBAdapter, IBConnectionSettings  # noqa: E402


async def main(args) -> int:
    settings = IBConnectionSettings(
        host=args.host, port=args.port, client_id=args.client_id
    )
    adapter = IBAdapter(args.account, settings, read_only=True)
    print(
        f"Connecting to {args.host}:{args.port} as client {args.client_id} (read-only)..."
    )
    try:
        await adapter.connect()
    except BrokerError as exc:
        print(f"FAILED: {exc}")
        print(
            "Checklist: Gateway running and logged in? API enabled? Port correct? "
            "127.0.0.1 in Trusted IPs? Client ID not already in use?"
        )
        return 1
    ib = adapter.ib
    print("OK: connected")
    print(f"  Accounts on this login: {', '.join(adapter.health.managed_accounts)}")
    print(
        f"  Account checked:       {args.account} ({'live' if adapter.is_live else 'paper'})"
    )
    print(f"  Server version:        {ib.client.serverVersion()}")
    print(f"  IB server time:        {await ib.reqCurrentTimeAsync()}")

    floor = load_trading_config().margin_floor
    snap = None
    for _ in range(20):  # account values arrive just after connecting
        try:
            snap = await adapter.account_snapshot()
            break
        except BrokerError:
            await asyncio.sleep(0.5)
    if snap is None:
        print("WARNING: no account values received yet (try again in a minute)")
    else:
        el_ok = snap.excess_liquidity >= floor.entry_min_excess_liquidity
        cushion_ok = snap.cushion >= floor.cushion_min_new_trade
        print("\nAccount")
        print(f"  Net liquidation:       ${snap.net_liquidation:,.0f}")
        print(
            f"  Excess liquidity:      ${snap.excess_liquidity:,.0f}   "
            f"(floor ${floor.excess_liquidity_min:,.0f}; new trades need "
            f"${floor.entry_min_excess_liquidity:,.0f}) {'OK' if el_ok else 'BELOW'}"
        )
        print(
            f"  Cushion:               {snap.cushion:.1%}   (new trades need "
            f"{floor.cushion_min_new_trade:.0%}) {'OK' if cushion_ok else 'BELOW'}"
        )
        print(f"  Maintenance margin:    ${snap.maint_margin:,.0f}")
        print(f"  Initial margin:        ${snap.init_margin:,.0f}")
        print(f"  Available funds:       ${snap.available_funds:,.0f}")
        print(f"  Cash:                  ${snap.cash:,.0f}")

    positions = await adapter.positions()
    options = [p for p in positions if isinstance(p.contract, OptionContract)]
    stocks = [p for p in positions if not isinstance(p.contract, OptionContract)]
    print(f"\nPositions: {len(stocks)} stock/ETF, {len(options)} option")
    for p in sorted(options, key=lambda p: p.contract.expiry)[:15]:
        price = f"{p.market_price:.2f}" if p.market_price is not None else "n/a"
        style = "European" if not p.contract.can_be_assigned_early else "American"
        print(
            f"  {p.contract.key:<28} qty {p.quantity:>5g}  cost {p.avg_cost:>8.2f}  "
            f"price {price:>8}  {style}"
        )
    if len(options) > 15:
        print(f"  ... and {len(options) - 15} more")
    if adapter.unsupported_positions:
        print(f"  Not handled by the bot: {', '.join(adapter.unsupported_positions)}")

    print("\nMarket data")
    try:
        for q in await adapter.quotes([StockContract("SPY"), StockContract("QQQM")]):
            kind = (
                "DELAYED (check market data subscriptions / paper sharing)"
                if q.delayed
                else "live"
            )
            print(f"  {q.contract.key:<6} bid {q.bid}  ask {q.ask}  ({kind})")
        chains = await adapter.option_chain("XSP")
        for chain in chains:
            print(
                f"  XSP options ({chain.trading_class}): {len(chain.expiries)} expiries, "
                f"{len(chain.strikes)} strikes, next {chain.expiries[0] if chain.expiries else '-'}"
            )
    except BrokerError as exc:
        print(f"  FAILED: {exc}")
    await adapter.disconnect()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--account", default=os.environ.get("TRADING_ACCOUNT_ID"), required=False
    )
    parser.add_argument("--host", default=os.environ.get("IB_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("IB_PORT", 4002))
    )
    parser.add_argument(
        "--client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", 3))
    )
    parsed = parser.parse_args()
    if not parsed.account:
        parser.error("--account is required (or set TRADING_ACCOUNT_ID)")
    sys.exit(asyncio.run(main(parsed)))
