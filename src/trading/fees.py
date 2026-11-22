"""Fee and financing model (plan T21, spec §5.8, §7.5, R2).

Every decision uses **net** numbers: premium minus estimated commission,
exchange and regulatory fees, and the margin interest on any debit (the
account has no cash, R8, so a debit is borrowed).

* :func:`estimate_order_fees`: a priori estimate per order.
* :class:`FeeCalibrator`: learns the actual all-in cost per contract per
  option root from journaled fills (IB's commission reports include exchange
  and regulatory fees), and replaces the estimate once it has enough fills.
* :func:`margin_interest`: interest on negative cash for a number of days.

Expiration and assignment cost nothing, which is why holding to expiry is
the default exit (§5.8).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from src.trading.config import FeesConfig
from src.trading.domain import Fill, OptionContract, OrderRequest, StockContract


def option_root(contract) -> str:
    if isinstance(contract, OptionContract):
        return (contract.trading_class or contract.underlying).upper()
    return contract.symbol.upper()


def commission_per_contract(premium: float, cfg: FeesConfig) -> float:
    premium = abs(premium)
    if premium < 0.05:
        return cfg.per_contract_tiny
    if premium < 0.10:
        return cfg.per_contract_cheap
    return cfg.per_contract


@dataclass(frozen=True)
class FeeEstimate:
    commission: float
    exchange: float
    regulatory: float
    calibrated: bool = False

    @property
    def total(self) -> float:
        return self.commission + self.exchange + self.regulatory


class FeeCalibrator:
    """Average all-in fee per contract per root, from real fills."""

    def __init__(self, cfg: FeesConfig):
        self.cfg = cfg
        self._fees: Dict[str, float] = defaultdict(float)
        self._contracts: Dict[str, int] = defaultdict(int)
        self._fills: Dict[str, int] = defaultdict(int)

    def add_fills(self, fills: Iterable[Fill]) -> None:
        for f in fills:
            if not isinstance(f.contract, OptionContract) or f.quantity <= 0:
                continue
            root = option_root(f.contract)
            self._fees[root] += f.commission
            self._contracts[root] += f.quantity
            self._fills[root] += 1

    def per_contract(self, root: str) -> Optional[float]:
        root = root.upper()
        if self._fills.get(root, 0) < self.cfg.calibration_min_fills:
            return None
        return self._fees[root] / self._contracts[root]


def estimate_order_fees(
    order: OrderRequest,
    leg_premiums: Optional[Dict[str, float]] = None,
    cfg: FeesConfig = FeesConfig(),
    calibrator: Optional[FeeCalibrator] = None,
) -> FeeEstimate:
    """Estimated fees for one order (all legs, all contracts).

    ``leg_premiums``: contract key -> expected fill price per share; it picks
    the commission tier. Missing premiums use the standard $0.65 tier.
    """
    leg_premiums = leg_premiums or {}
    commission = exchange = regulatory = 0.0
    calibrated_all = True
    for leg in order.legs:
        c = leg.contract
        if isinstance(c, StockContract):
            commission += max(
                0.0035 * leg.quantity, 0.35
            )  # IBKR Pro tiered stock, approx
            calibrated_all = False
            continue
        root = option_root(c)
        learned = calibrator.per_contract(root) if calibrator else None
        if learned is not None:
            commission += learned * leg.quantity  # all-in, includes exchange/regulatory
            continue
        calibrated_all = False
        premium = leg_premiums.get(c.key, 1.0)
        commission += commission_per_contract(premium, cfg) * leg.quantity
        exchange += (
            cfg.index_exchange_per_contract.get(root, cfg.exchange_per_contract)
            * leg.quantity
        )
        regulatory += cfg.regulatory_per_contract * leg.quantity
    if not calibrated_all:
        commission = max(commission, cfg.min_per_order)
    return FeeEstimate(commission, exchange, regulatory, calibrated=calibrated_all)


def margin_interest(cash: float, days: float, cfg: FeesConfig = FeesConfig()) -> float:
    """Interest owed on negative cash (a debit balance) over ``days``."""
    if cash >= 0 or days <= 0:
        return 0.0
    return -cash * cfg.margin_rate_annual * days / 360.0  # IB uses a 360-day year


def net_credit(
    order: OrderRequest,
    leg_premiums: Optional[Dict[str, float]] = None,
    cfg: FeesConfig = FeesConfig(),
    calibrator: Optional[FeeCalibrator] = None,
) -> float:
    """Dollars received after fees for a credit order (negative for a debit).

    ``order.limit_price`` is per share, negative for a credit; contracts
    have the legs' multiplier (100 for equity and index options).
    """
    qty = order.legs[0].quantity
    multiplier = getattr(order.legs[0].contract, "multiplier", 1)
    gross = -order.limit_price * qty * multiplier
    return gross - estimate_order_fees(order, leg_premiums, cfg, calibrator).total
