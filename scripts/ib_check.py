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
