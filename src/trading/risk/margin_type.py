"""Detect Reg-T vs Portfolio Margin from IB what-if (plan T18b, spec §3, §4.3).

IB's account values don't reliably say which margin model an account uses,
so the bot measures it. It asks IB for the margin of a reference trade (one
short put about 10% out of the money) and compares that with the Reg-T
formula:

    stock/ETF: max(20% x underlying - OTM amount, 10% x strike) + premium
    broad index: same with 15% instead of 20%

* IB's number close to the formula (within +/-35%): **Reg-T**.
* Well below it (< 60%): **Portfolio Margin**.
* Otherwise: **unknown**, and the bot keeps the configured type and warns.

The engine refuses to trade if the detected type contradicts ``margin_type``
in the config (spec §4.3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.trading.config import MarginType
from src.trading.domain import AccountSnapshot, MarginImpact, OptionContract, Right

BROAD_INDEXES = {"SPX", "SPXW", "XSP", "NDX", "XND", "RUT", "MRUT", "DJX"}


def reg_t_short_option_margin(
    option: OptionContract, underlying: float, premium: float
) -> float:
    """Reg-T margin for one uncovered short option contract (per contract, $)."""
    pct = 0.15 if option.underlying.upper() in BROAD_INDEXES else 0.20
    if option.right is Right.PUT:
        otm = max(0.0, underlying - option.strike)
        floor = 0.10 * option.strike
    else:
        otm = max(0.0, option.strike - underlying)
        floor = 0.10 * underlying
    per_share = max(pct * underlying - otm, floor) + premium
    return per_share * option.multiplier


@dataclass(frozen=True)
class MarginTypeResult:
    detected: Optional[MarginType]
    measured: float  # IB's initial margin increase for the reference trade
    reg_t_expected: float
    ratio: float

    @property
    def summary(self) -> str:
        kind = self.detected.value if self.detected else "unknown"
        return (
            f"{kind}: IB margin ${self.measured:,.0f} vs Reg-T formula "
            f"${self.reg_t_expected:,.0f} (ratio {self.ratio:.2f})"
        )


def classify_margin_type(
    account: AccountSnapshot,
    impact: MarginImpact,
    option: OptionContract,
    underlying: float,
    premium: float,
    contracts: int = 1,
) -> MarginTypeResult:
    # Prefer IB's own change figure: the account snapshot can be ~3 min old,
    # and stock margin moves with the market in the meantime.
    if impact.init_margin_change is not None:
        measured = impact.init_margin_change
    else:
        measured = impact.init_margin_after - account.init_margin
    expected = reg_t_short_option_margin(option, underlying, premium) * contracts
    ratio = measured / expected if expected > 0 else 0.0
    if 0.65 <= ratio <= 1.35:
        detected = MarginType.REG_T
    elif 0 < ratio < 0.60:
        detected = MarginType.PORTFOLIO
    else:
        detected = None
    return MarginTypeResult(detected, measured, expected, ratio)
