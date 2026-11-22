"""Assignment & Event Guard: roll triggers (plan T28a, spec §5.11, R7).

R7: no assignments. Every short option is checked each cycle, and any of
these triggers raises a **signal** to roll (or close):

| Trigger | Fires when | Urgency |
|---|---|---|
| ``early_roll`` | abs(delta) >= put_roll_delta (0.35) / call_roll_delta (0.40; per symbol, e.g. QQQM 0.30) | normal |
| ``low_extrinsic`` | American option, ITM, time value < $0.15 or < 0.5% of strike: early-assignment zone | high |
| ``pin_risk`` | <= 5 DTE and the stock within 2% of the strike | high |
| ``ex_dividend`` | short call ITM, ex-date before expiry, time value < dividend + $0.05; roll by 15:30 ET the day before | high |
| ``ex_dividend_unknown`` | short call ITM, next ex-date unknown | warning |
| ``earnings`` | earnings before expiry and abs(delta) > 0.25; act the day before | high |
| ``expiration_day`` | expires today and ITM or within 1% of the strike; roll or close by 15:00 ET | critical |

Index options (XSP/SPX) can't be assigned, but expiration-day and early-roll
triggers still apply: a loss should be taken on purpose, not at settlement.

This module only **detects**. T28b builds and sends the rolls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional

from src.trading.config import TradingConfig
from src.trading.domain import OptionContract, Position, Quote, Right

SEVERITY_ORDER = {"normal": 0, "warning": 1, "high": 2, "critical": 3}


@dataclass(frozen=True)
class GuardSignal:
    key: str
    trigger: str
    severity: str  # normal | warning | high | critical
    detail: str
    deadline: str = ""  # human-readable "act by"

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "trigger": self.trigger,
            "severity": self.severity,
            "detail": self.detail,
            "deadline": self.deadline,
        }


def _call_roll_delta(config: TradingConfig, underlying: str) -> float:
    entry = config.watchlist.get(underlying.upper())
    if entry is not None and entry.call_roll_delta is not None:
        return entry.call_roll_delta
    return config.guard.call_roll_delta


def check_position(
    pos: Position,
    quote: Optional[Quote],
    today: date,
    config: TradingConfig,
    events=None,
) -> List[GuardSignal]:
    c = pos.contract
    if not isinstance(c, OptionContract) or pos.quantity >= 0:
        return []
    g = config.guard
    key = c.key
    und = quote.underlying_price if quote else None
    delta = abs(quote.greeks.delta) if quote and quote.greeks else None
    dte = c.days_to_expiry(today)
    itm = und is not None and c.is_itm(und)
    extrinsic = quote.extrinsic() if quote else None
    out: List[GuardSignal] = []

    # Expiration day: nothing ITM or near the money is left to expire.
    if dte == 0 and und is not None and (itm or abs(und - c.strike) / c.strike <= 0.01):
        out.append(
            GuardSignal(
                key,
                "expiration_day",
                "critical",
                f"expires today; stock {und:g} vs strike {c.strike:g}",
                "roll or close by 15:00 ET today",
            )
        )

    # Delta-based early roll, while a credit roll is still available.
    threshold = (
        g.put_roll_delta
        if c.right is Right.PUT
        else _call_roll_delta(config, c.underlying)
    )
    if delta is not None and delta >= threshold:
        out.append(
            GuardSignal(
                key, "early_roll", "normal", f"delta {delta:.2f} >= {threshold:.2f}"
            )
        )

    # Early-assignment zone (American options only).
    if c.can_be_assigned_early and itm and extrinsic is not None:
        if extrinsic < g.min_extrinsic or extrinsic < 0.005 * c.strike:
            out.append(
                GuardSignal(
                    key,
                    "low_extrinsic",
                    "high",
                    f"ITM with only ${extrinsic:.2f} time value",
                )
            )

    # Pin risk.
    if (
        und is not None
        and 0 < dte <= g.pin_dte
        and abs(und - c.strike) / c.strike <= g.pin_pct
    ):
        out.append(
            GuardSignal(
                key,
                "pin_risk",
                "high",
                f"{dte} DTE, stock within {g.pin_pct:.0%} of the strike",
            )
        )

    if events is not None and c.can_be_assigned_early:
        # Ex-dividend early assignment (short calls).
        if c.right is Right.CALL and itm:
            risk = events.ex_dividend_risk(c.underlying, c.expiry, today)
            if risk is None:
                out.append(
                    GuardSignal(
                        key,
                        "ex_dividend_unknown",
                        "warning",
                        "ITM short call; next ex-dividend date unknown",
                    )
                )
            elif risk:
                div = events.ex_dividend_before(c.underlying, c.expiry, today)
                if div is not None and (
                    extrinsic is None or extrinsic < div.amount + 0.05
                ):
                    out.append(
                        GuardSignal(
                            key,
                            "ex_dividend",
                            "high",
                            f"ex-dividend {div.ex_date} (${div.amount:.2f}) before expiry",
                            "roll by 15:30 ET the day before the ex-date",
                        )
                    )
        # Earnings before expiry.
        if delta is not None and delta > g.earnings_delta:
            if events.earnings_before(c.underlying, c.expiry, today):
                out.append(
                    GuardSignal(
                        key,
                        "earnings",
                        "high",
                        f"earnings before expiry, delta {delta:.2f}",
                        "roll past earnings or close the day before",
                    )
                )
    return out


def scan(
    positions: Iterable[Position],
    quotes: Dict[str, Quote],
    today: date,
    config: TradingConfig,
    events=None,
) -> List[GuardSignal]:
    """All signals, most severe first."""
    signals: List[GuardSignal] = []
    for pos in positions:
        signals.extend(
            check_position(pos, quotes.get(pos.contract.key), today, config, events)
        )
    return sorted(signals, key=lambda s: -SEVERITY_ORDER[s.severity])
