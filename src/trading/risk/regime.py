"""Volatility-regime sizing (plan T19b, spec §6.6).

The risk budget (10% of NLV lost in the -20% / IV +50% scenario) is
multiplied by a regime factor:

| Regime | When | Multiplier | Entries |
|---|---|---|---|
| low | VIX < 13 (thin premium, complacency) | 0.5 | lowest deltas only |
| normal | 13 <= VIX < 20 | 0.8 | normal |
| elevated | 20 <= VIX <= 30 | 1.0 | normal, best edge |
| rising | VIX > 30 and above its 5-day average | 0 | none: guards and hedge only |
| fading | VIX > 30 and >= 15% below its 5-day high | 0.6 | put spreads only, delta <= 0.10 |

VIX above 30 that is neither clearly rising nor 15% off its high counts as
**rising** (no new risk): the conservative reading of an unsettled market.

An options-book drawdown halves the result: from 5% of NLV until it
recovers to within 2% (hysteresis), and a drawdown of 8% halts opening (§6.4).
Unknown VIX means no new risk (multiplier 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from src.trading.config import RegimeConfig, RiskConfig


@dataclass(frozen=True)
class Regime:
    name: str
    multiplier: float
    spreads_only: bool = False
    max_delta: Optional[float] = None
    vix: Optional[float] = None
    detail: str = ""


def classify_regime(
    vix: Optional[float], recent: Sequence[float], cfg: RegimeConfig
) -> Regime:
    """``recent``: the last 5 daily VIX closes (oldest first), not including today."""
    m = cfg.mult
    if vix is None or vix <= 0:
        return Regime("unknown", 0.0, detail="no VIX data: no new risk")
    if vix < cfg.vix_low:
        return Regime(
            "low",
            m.low,
            max_delta=0.10,
            vix=vix,
            detail=f"VIX {vix:.1f} < {cfg.vix_low:g}",
        )
    if vix < cfg.vix_mid:
        return Regime("normal", m.normal, vix=vix, detail=f"VIX {vix:.1f}")
    if vix <= cfg.vix_high:
        return Regime("elevated", m.elevated, vix=vix, detail=f"VIX {vix:.1f}")
    if not recent:
        return Regime(
            "rising",
            m.rising,
            vix=vix,
            detail=f"VIX {vix:.1f} > {cfg.vix_high:g}, no history",
        )
    avg5, high5 = sum(recent) / len(recent), max(list(recent) + [vix])
    if vix <= high5 * (1 - cfg.fade_from_high):
        return Regime(
            "fading",
            m.fading,
            spreads_only=True,
            max_delta=0.10,
            vix=vix,
            detail=f"VIX {vix:.1f} is {1 - vix / high5:.0%} below its 5-day high {high5:.1f}",
        )
    trend = "above" if vix > avg5 else "not yet 15% below the high, at/below"
    return Regime(
        "rising",
        m.rising,
        vix=vix,
        detail=f"VIX {vix:.1f} {trend} its 5-day average {avg5:.1f}",
    )


@dataclass(frozen=True)
class BudgetState:
    regime: Regime
    drawdown: float  # options-book drawdown, fraction of NLV (>= 0)
    reduced: bool  # drawdown halving active
    halted: bool  # drawdown beyond the halt level
    multiplier: float  # final budget multiplier

    def budget(self, nlv: float, risk: RiskConfig) -> float:
        return risk.risk_budget_pct_nlv * self.multiplier * nlv


def budget_state(
    regime: Regime, drawdown: float, previously_reduced: bool, risk: RiskConfig
) -> BudgetState:
    """Apply the drawdown rules on top of the regime multiplier."""
    reduced = drawdown >= risk.options_dd_reduce or (
        previously_reduced and drawdown > risk.options_dd_recover
    )
    halted = drawdown >= risk.options_dd_halt
    mult = 0.0 if halted else regime.multiplier * (0.5 if reduced else 1.0)
    return BudgetState(regime, drawdown, reduced, halted, mult)


def vix_inputs(closes: Sequence[float]) -> tuple:
    """(current VIX, previous 5 closes) from daily bars where the last is today."""
    if not closes:
        return None, []
    return closes[-1], list(closes[-6:-1])
