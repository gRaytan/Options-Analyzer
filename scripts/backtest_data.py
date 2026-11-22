#!/usr/bin/env python3
"""Backtest data: fetch history, show synthetic chains, calibrate against IB (plan T36).

Usage (from the repository root):

    # 1. Daily bars, on the computer that runs IB Gateway (read-only):
    python scripts/backtest_data.py fetch --source ib --account DU1234567
    #    (or --source yfinance where Yahoo isn't blocked; CBOE VIX data is always fetched)

    # 2. Look at a synthetic chain on any past day:
    python scripts/backtest_data.py chain --symbol XSP --day 2020-03-16

    # 3. Calibration: record today's live XSP chain, then compare after the close
    #    (when CBOE has published today's VIX):
    python scripts/backtest_data.py snapshot --symbol XSP --account DU1234567
    python scripts/backtest_data.py compare data/backtest/recorded/XSP_2026-11-23.csv

Everything is stored under data/backtest/ (git-ignored). Nothing here places orders.
"""
import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading.backtest import data  # noqa: E402
from src.trading.backtest.synthetic import (  # noqa: E402
    SyntheticMarket,
    compare_quotes,
)
from src.trading.domain import Bar, Right  # noqa: E402

DEFAULT_SYMBOLS = ["SPY", "QQQ", "QQQM", "SPX"]


def _adapter(args):
    from src.trading.broker.ib_adapter import (  # pylint: disable=import-outside-toplevel
        IBAdapter,
        IBConnectionSettings,
    )

    settings = IBConnectionSettings(
        host=args.host, port=args.port, client_id=args.client_id
    )
    return IBAdapter(args.account, settings, read_only=True)


async def cmd_fetch(args) -> int:
    for name in data.VOL_INDICES:
        series = data.fetch_cboe(name, refresh=True)
        first = min(series) if series else None
        print(f"CBOE {name:6s} {len(series):5d} days from {first}")
    adapter = None
    if args.source == "ib":
        adapter = _adapter(args)
        await adapter.connect()
    try:
        for sym in args.symbols:
            if args.source == "ib":
                bars = await data.fetch_ib(adapter, sym, args.years)
            else:
                start = date.today() - timedelta(days=365 * args.years)
                bars = data.fetch_yfinance(sym, start)
            if not bars:
                print(f"{sym}: no bars")
                continue
            path = data.save_bars(sym, bars)
            print(
                f"{sym:5s} {len(bars):5d} bars {bars[0].day} .. {bars[-1].day} -> {path}"
            )
    finally:
        if adapter is not None:
            await adapter.disconnect()
    return 0


def _band(market, symbol, day, dte_min, dte_max):
    chain = market.chain(symbol, day)
    if not chain:
        return []
    exps = [e for e in chain[0].expiries if dte_min <= (e - day).days <= dte_max]
    return [
        market.contract(symbol, e, k, Right.PUT)
        for e in exps
        for k in chain[0].strikes
        if k < market.price(symbol, day)
    ]


def cmd_chain(args) -> int:
    sources = ["SPX", "SPY"] if args.symbol.upper() == "XSP" else [args.symbol]
    have = [x for x in sources if data.bars_path(x).exists()] or sources
    market = SyntheticMarket(data.load_history(have))
    day = date.fromisoformat(args.day)
    price = market.price(args.symbol, day)
    if price is None:
        print(
            f"no {args.symbol} bar on {day} (fetch bars first, or pick a trading day)"
        )
        return 1
    vix = market.history.vol_on("VIX", day)
    print(
        f"{args.symbol} {day}: price {price:.2f}, VIX {vix * 100 if vix else float('nan'):.1f}"
    )
    print(f"{'contract':28s} {'bid':>7s} {'ask':>7s} {'iv':>6s} {'delta':>7s}")
    for q in market.quotes(_band(market, args.symbol, day, 30, 45), day):
        d = abs(q.greeks.delta)
        if 0.04 <= d <= 0.30:
            print(
                f"{q.contract.key:28s} {q.bid:7.2f} {q.ask:7.2f} "
                f"{q.greeks.iv * 100:5.1f}% {q.greeks.delta:7.3f}"
            )
    for note in sorted(market.fallbacks):
        print("note:", note)
    return 0


async def cmd_snapshot(args) -> int:
    adapter = _adapter(args)
    await adapter.connect()
    try:
        day = date.today()
        chains = await adapter.option_chain(args.symbol)
        chain = next((c for c in chains if c.trading_class == args.symbol), chains[0])
        from src.trading.domain import (  # pylint: disable=import-outside-toplevel
            HistoryKind,
        )

        # Pick strikes around the latest close; each option quote then carries
        # IB's own underlying price, which is what the comparison uses.
        price = (await adapter.daily_history(args.symbol, HistoryKind.PRICE, 5))[
            -1
        ].close
        exps = [e for e in chain.expiries if 25 <= (e - day).days <= 50][:2]
        ks = [k for k in chain.strikes if price * 0.85 <= k < price][::-1][:40]
        from src.trading.scanner import (  # pylint: disable=import-outside-toplevel
            candidate_contracts,
        )

        contracts = [
            c
            for c in candidate_contracts(
                args.symbol,
                Right.PUT,
                [chain],
                price,
                day,
                25,
                50,
                chain.trading_class,
                80,
            )
            if c.expiry in exps and c.strike in ks
        ]
        quotes = await adapter.quotes(contracts)
        out = data.default_dir() / "recorded" / f"{args.symbol}_{day.isoformat()}.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(data.recorded_to_csv(quotes, day))
        print(f"{len(quotes)} quotes, {args.symbol} at {price:.2f} -> {out}")
    finally:
        await adapter.disconnect()
    return 0


def cmd_compare(args) -> int:
    with open(args.file, encoding="utf-8") as fh:
        day, und, recorded = data.recorded_from_csv(fh.read())
    if not recorded:
        print("no quotes in the file")
        return 1
    sym = recorded[0].contract.underlying
    hist = data.load_history([])
    hist.bars[sym] = [Bar(day, und, und, und, und)]  # the price at snapshot time
    market = SyntheticMarket(hist)
    if market.history.vol_on("VIX", day) is None:
        print(f"CBOE has no VIX close for {day} yet: run again after the close")
        return 1
    synth = {
        q.contract.key: q for q in market.quotes([r.contract for r in recorded], day)
    }
    cmp_ = compare_quotes(recorded, synth)
    print(f"{cmp_.n} options compared ({sym} {day}, price {und:.2f})")
    print(f"  mean |error|     ${cmp_.mean_abs_error:.3f} per share")
    print(f"  mean |error| %   {cmp_.mean_rel_error:.1%} of the IB mid")
    print(
        f"  mean IV error    {cmp_.mean_iv_error:+.2f} vol points (negative = synthetic cheaper, conservative)"
    )
    print(f"  worst            {cmp_.worst[0]} ({cmp_.worst[1]:+.2f})")
    print(
        "OK"
        if cmp_.within()
        else "Outside 25%: send me this output to recalibrate the skew"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    def ib_args(p):
        p.add_argument("--account", default=os.environ.get("TRADING_ACCOUNT_ID"))
        p.add_argument("--host", default=os.environ.get("IB_HOST", "127.0.0.1"))
        p.add_argument("--port", type=int, default=int(os.environ.get("IB_PORT", 4002)))
        p.add_argument(
            "--client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", 4))
        )

    f = sub.add_parser("fetch", help="download daily bars and the CBOE vol indices")
    f.add_argument("--source", choices=["ib", "yfinance"], default="ib")
    f.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    f.add_argument("--years", type=int, default=18)
    ib_args(f)
    c = sub.add_parser("chain", help="print a synthetic put chain (30-45 DTE)")
    c.add_argument("--symbol", default="XSP")
    c.add_argument("--day", required=True)
    s = sub.add_parser("snapshot", help="record today's live IB put chain")
    s.add_argument("--symbol", default="XSP")
    ib_args(s)
    m = sub.add_parser(
        "compare", help="compare a recorded chain with the synthetic one"
    )
    m.add_argument("file")
    args = parser.parse_args()

    if args.cmd in ("fetch", "snapshot") and getattr(args, "source", "ib") == "ib":
        if not args.account:
            parser.error("--account is required for IB (or set TRADING_ACCOUNT_ID)")
    if args.cmd == "fetch":
        return asyncio.run(cmd_fetch(args))
    if args.cmd == "snapshot":
        return asyncio.run(cmd_snapshot(args))
    if args.cmd == "chain":
        return cmd_chain(args)
    return cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
