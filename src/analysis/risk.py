"""Risk analysis for option positions."""
from typing import List, Dict, Any
from dataclasses import dataclass, field

from src.models.position import OptionPosition
from src.models.portfolio import Portfolio


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
    
    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize risk analyzer with configuration.
        
        Args:
            config: Configuration dict with thresholds
        """
        self.config = config or {}
        self.high_delta_threshold = self.config.get("high_delta_threshold", 0.6)
        self.days_to_expiration_warning = self.config.get("days_to_expiration_warning", 7)
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
            risk.flags.append(RiskFlag(
                flag_type="HIGH_DELTA_SHORT_CALL",
                severity="high",
                message=f"Short call with high delta ({abs(option.delta):.2f} > {self.high_delta_threshold})",
                value=abs(option.delta)
            ))
            risk.risk_score += 30
        
        # Check for ITM options (assignment risk)
        if option.is_itm:
            severity = "high" if option.days_to_expiration <= 7 else "medium"
            risk.flags.append(RiskFlag(
                flag_type="ITM_OPTION",
                severity=severity,
                message=f"Option is in-the-money (moneyness: {option.moneyness:.2%})",
                value=option.moneyness
            ))
            risk.risk_score += 20 if severity == "high" else 10
        
        # Check for expiring soon
        if option.days_to_expiration <= self.days_to_expiration_warning:
            risk.flags.append(RiskFlag(
                flag_type="EXPIRING_SOON",
                severity="medium",
                message=f"Option expires in {option.days_to_expiration} days",
                value=option.days_to_expiration
            ))
            risk.risk_score += 15
        
        # Check for profit target reached
        if option.cost_price > 0:
            profit_pct = (option.cost_price - option.current_price) / option.cost_price * 100
            if profit_pct >= self.profit_target_percentage:
                risk.flags.append(RiskFlag(
                    flag_type="PROFIT_TARGET_REACHED",
                    severity="low",
                    message=f"Profit target reached ({profit_pct:.1f}% > {self.profit_target_percentage}%)",
                    value=profit_pct
                ))
        
        # Check for large unrealized loss
        if option.unrealized_pnl < -500:  # More than $500 loss
            loss_pct = abs(option.unrealized_pnl / option.cost_basis * 100) if option.cost_basis != 0 else 0
            severity = "high" if loss_pct > 100 else "medium"
            risk.flags.append(RiskFlag(
                flag_type="LARGE_LOSS",
                severity=severity,
                message=f"Large unrealized loss: ${abs(option.unrealized_pnl):,.2f} ({loss_pct:.1f}%)",
                value=option.unrealized_pnl
            ))
            risk.risk_score += 25 if severity == "high" else 15
        
        # Update option flags
        option.flags = [f.flag_type for f in risk.flags]
        
        return risk
    
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
            "high_delta_calls": sum(1 for r in portfolio_risk.position_risks 
                                    if any(f.flag_type == "HIGH_DELTA_SHORT_CALL" for f in r.flags)),
            "itm_options": sum(1 for r in portfolio_risk.position_risks 
                              if any(f.flag_type == "ITM_OPTION" for f in r.flags)),
            "expiring_soon": sum(1 for r in portfolio_risk.position_risks 
                                if any(f.flag_type == "EXPIRING_SOON" for f in r.flags)),
            "profit_targets_reached": sum(1 for r in portfolio_risk.position_risks 
                                          if any(f.flag_type == "PROFIT_TARGET_REACHED" for f in r.flags)),
        }
        
        return portfolio_risk


def analyze_portfolio_risk(portfolio: Portfolio, config: Dict[str, Any] = None) -> PortfolioRisk:
    """Convenience function to analyze portfolio risk."""
    analyzer = RiskAnalyzer(config)
    return analyzer.analyze_portfolio(portfolio)

