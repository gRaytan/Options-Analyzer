"""Covered calls on shares held at another broker (e.g. a cash account elsewhere).

The bot can't trade there, so it **recommends** and **watches**; the owner
places the orders and records them on the "Other broker" page:

* **Recommendation** (once a day, in market hours): for each holding in
  ``data/config/external_holdings.json`` with ``cc_calls`` set, if fewer calls
  are open than wanted, the call to sell now: 30-45 days out, delta closest to
  ``cc_delta`` (0.15), with its live bid / mid from IB's market data and the
  premium for the missing contracts.
* **Watch** (every scan): each recorded open call is quoted; an alert asks to
  **roll** (up and out, for a credit; the owner's no-assignment rule) when its
  delta reaches ``roll_delta`` (0.35), or when it's within 5 days of expiry and
  in the money. Calls past expiry stop counting.

Holdings file entry, e.g.::

    "AAPL": {"shares": 500, "cc_calls": 2, "cc_delta": 0.15, "roll_delta": 0.35}
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from src.trading.domain import OptionContract, Quote, Right

DTE_RANGE = (30, 45)
ROLL_DTE = 5


@dataclass(frozen=True)
class Holding:
    symbol: str
    shares: float
    cc_calls: int = 0
    cc_delta: float = 0.15
    roll_delta: float = 0.35

    @property
    def max_calls(self) -> int:
        return int(self.shares // 100)


def holdings_from(data: dict) -> List[Holding]:
    out = []
    for sym, v in data.items():
        if sym.startswith("_") or not isinstance(v, dict):
            continue
        out.append(
            Holding(
                sym.upper(),
                float(v.get("shares", 0)),
                int(v.get("cc_calls", 0)),
                float(v.get("cc_delta", 0.15)),
                float(v.get("roll_delta", 0.35)),
            )
        )
    return out


@dataclass(frozen=True)
class ExternalCall:
    """A call the owner sold at the other broker (from the journal)."""

    trade_id: str
    contract: OptionContract
    contracts: int
    premium: float  # per share
    sold_on: date

    def is_open(self, today: date, closed: Sequence[str]) -> bool:
        return self.contract.expiry >= today and self.trade_id not in closed


def call_contract(symbol: str, expiry: date, strike: float) -> OptionContract:
    return OptionContract(symbol.upper(), expiry, float(strike), Right.CALL)


def record_id(symbol: str, expiry: date, strike: float, sold_on: date) -> str:
    raw = f"{symbol.upper()}|{expiry.isoformat()}|{strike:g}|{sold_on.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def open_calls(records: Sequence[dict], today: date) -> List[ExternalCall]:
    """Open calls from the journal's ``external_trades`` records."""
    sold, closed = [], set()
    for r in records:
        if r.get("action") == "sell":
            sold.append(
                ExternalCall(
                    r["trade_id"],
                    call_contract(
                        r["symbol"], date.fromisoformat(r["expiry"]), r["strike"]
                    ),
                    int(r["contracts"]),
                    float(r["price"]),
                    date.fromisoformat(r["date"]),
                )
            )
        elif r.get("action") in ("buy", "assigned", "expired"):
            closed.add(r.get("trade_id"))
    return [c for c in sold if c.is_open(today, closed)]


def candidate_expiries(expiries: Sequence[date], today: date) -> List[date]:
    lo, hi = DTE_RANGE
    return [e for e in expiries if lo <= (e - today).days <= hi]


@dataclass
class Recommendation:
    symbol: str
    contracts: int
    contract: Optional[OptionContract] = None
    quote: Optional[Quote] = None
    note: str = ""

    @property
    def premium(self) -> float:
        if self.quote is None or self.quote.bid is None:
            return 0.0
        return self.quote.bid * 100 * self.contracts

    def to_dict(self) -> dict:
        q = self.quote
        return {
            "symbol": self.symbol,
            "contracts": self.contracts,
            "key": self.contract.key if self.contract else None,
            "expiry": self.contract.expiry.isoformat() if self.contract else None,
            "strike": self.contract.strike if self.contract else None,
            "bid": q.bid if q else None,
            "mid": q.mid if q else None,
            "delta": q.greeks.delta if q and q.greeks else None,
            "underlying": q.underlying_price if q else None,
            "premium": self.premium,
            "note": self.note,
        }


def recommend(
    holding: Holding, open_: Sequence[ExternalCall], quotes: Sequence[Quote]
) -> Recommendation:
    """The call to sell now (delta closest to the target) for the missing contracts."""
    have = sum(c.contracts for c in open_ if c.contract.underlying == holding.symbol)
    want = min(holding.cc_calls, holding.max_calls)
    missing = max(0, want - have)
    rec = Recommendation(holding.symbol, missing)
    if missing == 0:
        rec.note = f"{have} call(s) open, the target is {want}: nothing to sell."
        return rec
    usable = [
        q
        for q in quotes
        if q.greeks is not None
        and q.bid
        and q.contract.right is Right.CALL
        and q.greeks.delta <= holding.cc_delta + 0.05
    ]
    if not usable:
        rec.note = "No call with a live quote and Greeks in the 30-45 day window."
        return rec
    best = min(usable, key=lambda q: abs(q.greeks.delta - holding.cc_delta))
    rec.contract, rec.quote = best.contract, best
    return rec


@dataclass
class RollAlert:
    call: ExternalCall
    reason: str
    quote: Optional[Quote]


def roll_alerts(
    holding: Holding,
    open_: Sequence[ExternalCall],
    quotes: Dict[str, Quote],
    today: date,
) -> List[RollAlert]:
    out = []
    for c in open_:
        if c.contract.underlying != holding.symbol:
            continue
        q = quotes.get(c.contract.key)
        if q is None or q.greeks is None:
            continue
        dte = (c.contract.expiry - today).days
        itm = q.underlying_price is not None and q.underlying_price >= c.contract.strike
        if q.greeks.delta >= holding.roll_delta:
            out.append(
                RollAlert(
                    c, f"delta {q.greeks.delta:.2f} >= {holding.roll_delta:.2f}", q
                )
            )
        elif dte <= ROLL_DTE and itm:
            out.append(RollAlert(c, f"in the money with {dte} days left", q))
    return out
