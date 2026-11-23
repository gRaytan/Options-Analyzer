"""Risk analysis for option positions.

Flags (spec §5.7 adds the last four groups for the trading bot, T24):

* HIGH_DELTA_SHORT_CALL / HIGH_DELTA_SHORT_PUT: delta past the threshold
* ITM_OPTION, EXPIRING_SOON, PROFIT_TARGET_REACHED
* LARGE_LOSS: the option now costs >= ``large_loss_multiple`` x the premium
  received (default 2x, the spec's stop level), not a fixed dollar amount
* PIN_RISK: <= ``pin_dte`` days to expiry and the stock within ``pin_pct``
  of the strike
* EX_DIVIDEND_ASSIGNMENT_RISK: short call ITM with an ex-dividend date before
  expiry and less time value than the dividend (early assignment is likely)
* EARNINGS_BEFORE_EXPIRY: earnings fall before expiry

The last two need an event calendar (``events``); without one they're skipped,
so the existing CSV-based pages behave as before.
"""
from datetime import date
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from src.models.position import OptionPosition
from src.models.portfolio import Portfolio


# Flags that mean "act on this position now" (positions page high-risk filter).
HIGH_RISK_FLAGS = frozenset(
    {
        "HIGH_DELTA_SHORT_CALL",
        "HIGH_DELTA_SHORT_PUT",
        "LARGE_LOSS",
        "PIN_RISK",
        "EX_DIVIDEND_ASSIGNMENT_RISK",
    }
)


@dataclass
class RiskFlag:
    """A risk flag for a position."""

    flag_type: str
    severity: str  # "high", "medium", "low"
    message: str
    value: float = 0.0


@dataclass
class PositionRisk:
    """Risk assessment for a single position."""

    option: OptionPosition
    flags: List[RiskFlag] = field(default_factory=list)
    risk_score: float = 0.0


@dataclass
class PortfolioRisk:
    """Risk assessment for the entire portfolio."""

    position_risks: List[PositionRisk] = field(default_factory=list)
    high_risk_positions: List[PositionRisk] = field(default_factory=list)
    total_risk_score: float = 0.0
    margin_utilization: float = 0.0
    summary: Dict[str, Any] = field(default_factory=dict)


class RiskAnalyzer:
    """Analyze risk for option positions."""

    def __init__(
        self, config: Dict[str, Any] = None, events=None, today: Optional[date] = None
    ):
        """
        Initialize risk analyzer with configuration.

        Args:
            config: Configuration dict with thresholds
            events: optional ``EventCalendar`` for earnings / ex-dividend flags
            today: date to evaluate against (defaults to today)
        """
        self.config = config or {}
        self.events = events
        self.today = today or date.today()
        self.high_delta_put_threshold = self.config.get(
            "high_delta_put_threshold", 0.35
        )
        self.large_loss_multiple = self.config.get("large_loss_multiple", 2.0)
        self.pin_dte = self.config.get("pin_dte", 5)
        self.pin_pct = self.config.get("pin_pct", 0.02)
        self.high_delta_threshold = self.config.get("high_delta_threshold", 0.6)
        self.days_to_expiration_warning = self.config.get(
            "days_to_expiration_warning", 7
        )
        self.profit_target_percentage = self.config.get("profit_target_percentage", 50)
        self.margin_warning_level = self.config.get("margin_warning_level", 0.8)

    def analyze_position(self, option: OptionPosition) -> PositionRisk:
        """Analyze risk for a single option position."""
        risk = PositionRisk(option=option)

        # Only analyze short positions for risk
        if not option.is_short:
            return risk

        # Check for high delta short calls (naked call risk)
        if option.is_call and abs(option.delta) > self.high_delta_threshold:
            risk.flags.append(
                RiskFlag(
                    flag_type="HIGH_DELTA_SHORT_CALL",
                    severity="high",
                    message=f"Short call with high delta ({abs(option.delta):.2f} > {self.high_delta_threshold})",
                    value=abs(option.delta),
                )
            )
            risk.risk_score += 30

        # High delta short puts: roll trigger (spec §5.11, put_roll_delta)
        if option.is_put and abs(option.delta) > self.high_delta_put_threshold:
            risk.flags.append(
                RiskFlag(
                    flag_type="HIGH_DELTA_SHORT_PUT",
                    severity="high" if abs(option.delta) >= 0.5 else "medium",
                    message=f"Short put with high delta ({abs(option.delta):.2f} > {self.high_delta_put_threshold})",
                    value=abs(option.delta),
                )
            )
            risk.risk_score += 25

        # Check for ITM options (assignment risk)
        if option.is_itm:
            severity = "high" if option.days_to_expiration <= 7 else "medium"
            risk.flags.append(
                RiskFlag(
                    flag_type="ITM_OPTION",
                    severity=severity,
                    message=f"Option is in-the-money (moneyness: {option.moneyness:.2%})",
                    value=option.moneyness,
                )
            )
            risk.risk_score += 20 if severity == "high" else 10

        # Check for expiring soon
        if option.days_to_expiration <= self.days_to_expiration_warning:
            risk.flags.append(
                RiskFlag(
                    flag_type="EXPIRING_SOON",
                    severity="medium",
                    message=f"Option expires in {option.days_to_expiration} days",
                    value=option.days_to_expiration,
                )
            )
            risk.risk_score += 15

        # Check for profit target reached
        if option.cost_price > 0:
            profit_pct = (
                (option.cost_price - option.current_price) / option.cost_price * 100
            )
            if profit_pct >= self.profit_target_percentage:
                risk.flags.append(
                    RiskFlag(
                        flag_type="PROFIT_TARGET_REACHED",
                        severity="low",
                        message=f"Profit target reached ({profit_pct:.1f}% > {self.profit_target_percentage}%)",
                        value=profit_pct,
                    )
                )

        # Large loss, relative to the premium received (spec §5.4/§5.7 stop: 2x credit)
        if (
            option.cost_price > 0
            and option.current_price >= self.large_loss_multiple * option.cost_price
        ):
            multiple = option.current_price / option.cost_price
            severity = "high" if multiple >= self.large_loss_multiple + 1 else "medium"
            risk.flags.append(
                RiskFlag(
                    flag_type="LARGE_LOSS",
                    severity=severity,
                    message=(
                        f"Option now {multiple:.1f}x the premium received "
                        f"(loss ${abs(option.unrealized_pnl):,.2f})"
                    ),
                    value=option.unrealized_pnl,
                )
            )
            risk.risk_score += 25 if severity == "high" else 15

        # Pin risk near expiry (spec §5.11)
        if (
            option.underlying_price > 0
            and option.strike > 0
            and option.days_to_expiration <= self.pin_dte
            and abs(option.underlying_price - option.strike) / option.strike
            <= self.pin_pct
        ):
            risk.flags.append(
                RiskFlag(
                    flag_type="PIN_RISK",
                    severity="high",
                    message=(
                        f"{option.days_to_expiration} days to expiry with the stock within "
                        f"{self.pin_pct:.0%} of the strike"
                    ),
                    value=option.underlying_price - option.strike,
                )
            )
            risk.risk_score += 20

        if self.events is not None and option.expiration is not None:
            self._event_flags(option, risk)

        # Update option flags
        option.flags = [f.flag_type for f in risk.flags]

        return risk

    def _event_flags(self, option: OptionPosition, risk: PositionRisk) -> None:
        """Earnings and ex-dividend flags (need the event calendar)."""
        if self.events.earnings_before(
            option.underlying, option.expiration, self.today
        ):
            risk.flags.append(
                RiskFlag(
                    flag_type="EARNINGS_BEFORE_EXPIRY",
                    severity="high" if abs(option.delta) > 0.25 else "medium",
                    message="Earnings fall before this option expires",
                )
            )
            risk.risk_score += 15
        if option.is_call and option.is_itm:
            div = self.events.ex_dividend_before(
                option.underlying, option.expiration, self.today
            )
            if div is not None and option.extrinsic_value < div.amount + 0.05:
                risk.flags.append(
                    RiskFlag(
                        flag_type="EX_DIVIDEND_ASSIGNMENT_RISK",
                        severity="high",
                        message=(
                            f"ITM short call with ex-dividend {div.ex_date} before expiry; "
                            f"time value ${option.extrinsic_value:.2f} < dividend ${div.amount:.2f}"
                        ),
                        value=div.amount,
                    )
                )
                risk.risk_score += 30

    def analyze_portfolio(self, portfolio: Portfolio) -> PortfolioRisk:
        """Analyze risk for the entire portfolio."""
        portfolio_risk = PortfolioRisk()

        for option in portfolio.options:
            position_risk = self.analyze_position(option)
            portfolio_risk.position_risks.append(position_risk)
            portfolio_risk.total_risk_score += position_risk.risk_score

            if position_risk.risk_score >= 20:
                portfolio_risk.high_risk_positions.append(position_risk)

        # Calculate margin utilization
        if portfolio.net_asset_value > 0:
            portfolio_risk.margin_utilization = (
                portfolio.total_margin_requirement / portfolio.net_asset_value
            )

        # Build summary
        portfolio_risk.summary = {
            "total_positions": len(portfolio.options),
            "high_risk_count": len(portfolio_risk.high_risk_positions),
            "total_risk_score": portfolio_risk.total_risk_score,
            "margin_utilization": portfolio_risk.margin_utilization,
            "high_delta_calls": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "HIGH_DELTA_SHORT_CALL" for f in r.flags)
            ),
            "high_delta_puts": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "HIGH_DELTA_SHORT_PUT" for f in r.flags)
            ),
            "pin_risk": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "PIN_RISK" for f in r.flags)
            ),
            "large_losses": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "LARGE_LOSS" for f in r.flags)
            ),
            "itm_options": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "ITM_OPTION" for f in r.flags)
            ),
            "expiring_soon": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "EXPIRING_SOON" for f in r.flags)
            ),
            "profit_targets_reached": sum(
                1
                for r in portfolio_risk.position_risks
                if any(f.flag_type == "PROFIT_TARGET_REACHED" for f in r.flags)
            ),
        }

        return portfolio_risk


def analyze_portfolio_risk(
    portfolio: Portfolio,
    config: Dict[str, Any] = None,
    events=None,
    today: Optional[date] = None,
) -> PortfolioRisk:
    """Convenience function to analyze portfolio risk."""
    analyzer = RiskAnalyzer(config, events=events, today=today)
    return analyzer.analyze_portfolio(portfolio)
