#!/usr/bin/env python3
"""Run the synthetic backtest (plan T37): the real engine, day by day, on past data.

First fetch the data once (see scripts/backtest_data.py fetch). Then:

    python scripts/backtest.py                          # full history + 2020 and 2022
    python scripts/backtest.py --start 2020-01-02 --end 2020-12-31
    python scripts/backtest.py --holdings SPY=0.6,QQQ=0.4 --nlv 1000000

Reports (Markdown + JSON + a daily CSV) go to data/backtest/reports/.
Nothing connects to IB and nothing is ordered: everything runs on a fake broker.
"""
import argparse
import csv
import logging
import os
import sys
from dataclasses import asdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.trading.backtest import data  # noqa: E402
from src.trading.backtest.engine import BacktestSettings, run_backtest  # noqa: E402
from src.trading.backtest.report import build_report, to_markdown  # noqa: E402
from src.trading.backtest.synthetic import SyntheticMarket  # noqa: E402
from src.trading.config import load_trading_config  # noqa: E402

PRESETS = {
    "2020 (COVID crash)": (date(2020, 1, 2), date(2020, 12, 31)),
    "2022 (bear market)": (date(2022, 1, 3), date(2022, 12, 30)),
}


def parse_holdings(text: str) -> dict:
    out = {}
    for part in text.split(","):
        sym, weight = part.split("=")
        out[sym.strip().upper()] = float(weight)
    if not 0 < sum(out.values()) <= 1.0 + 1e-9:
        raise ValueError("holding weights must add up to at most 1")
    return out


def run_one(market, config, settings, name, out_dir):
    def progress(i, n, rec):
        if i % 250 == 0 or i == n:
            nlv = f"NLV ${rec.nlv:,.0f}" if rec else ""
            print(f"  {name}: day {i}/{n} {rec.day if rec else ''} {nlv}", flush=True)

    work = out_dir / f"work-{settings.start}-{settings.end}"
    result = run_backtest(market, config, settings, workdir=work, progress=progress)
    report = build_report(result)
    stem = out_dir / f"backtest_{settings.start}_{settings.end}"
    stem.with_suffix(".md").write_text(to_markdown(report, name))
    stem.with_suffix(".json").write_text(report.to_json())
    with open(stem.with_suffix(".csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(result.days[0])))
        w.writeheader()
        for rec in result.days:
            w.writerow(asdict(rec))
    print(f"  -> {stem.with_suffix('.md')}")
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--nlv", type=float, default=1_000_000)
    p.add_argument("--holdings", default="SPY=0.5,QQQ=0.5")
    p.add_argument("--config", default="data/config/trading.json")
    p.add_argument("--fill-threshold", type=float, default=0.5)
    p.add_argument("--margin-rate", type=float, default=0.06)
    args = p.parse_args()
    logging.basicConfig(level=logging.ERROR)

    holdings = parse_holdings(args.holdings)
    symbols = sorted(set(holdings) | {"SPX", "SPY", "QQQ", "QQQM"})
    hist = data.load_history([s for s in symbols if data.bars_path(s).exists()])
    missing = [s for s in holdings if s not in hist.bars]
    if missing or "XSP" not in hist.bars:
        print(
            f"Missing bars for {missing or ['SPX/SPY']}: run scripts/backtest_data.py fetch first"
        )
        return 1
    if "VIX" not in hist.vol:
        print("No CBOE VIX history (network?): run scripts/backtest_data.py fetch")
        return 1
    market = SyntheticMarket(hist)
    config = load_trading_config(args.config)
    out_dir = data.default_dir() / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)

    first = max(hist.bars[s][0].day for s in [*holdings, "XSP"])
    last = min(hist.bars[s][-1].day for s in [*holdings, "XSP"])
    warm = date(
        first.year + 1, first.month, min(first.day, 28)
    )  # a year of history first
    runs = []
    if args.start or args.end:
        runs.append(
            ("Backtest", max(args.start or warm, warm), min(args.end or last, last))
        )
    else:
        runs.append((f"Full history {warm.year}-{last.year}", warm, last))
        for name, (s, e) in PRESETS.items():
            if s >= warm and e <= last:
                runs.append((name, s, e))
    for name, start, end in runs:
        print(f"{name}: {start} .. {end}")
        settings = BacktestSettings(
            start=start,
            end=end,
            nlv=args.nlv,
            holdings=holdings,
            margin_rate=args.margin_rate,
            fill_threshold=args.fill_threshold,
        )
        r = run_one(market, config, settings, name, out_dir)
        print(
            f"  return {r.total_return:+.1%} (shares only {r.baseline_return:+.1%}), "
            f"options {r.options_contribution_pct:+.1%}/yr, max DD {r.max_drawdown:.1%}, "
            f"win rate {r.win_rate:.0%}, days below floor {r.days_below_floor}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
