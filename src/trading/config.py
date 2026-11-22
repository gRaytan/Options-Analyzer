"""Trading configuration: schema, defaults and loading (spec §10).

The config lives in ``data/config/trading.json``. Every field has a safe
default, so a missing file yields a paper-mode config. Unknown keys are
rejected so typos can't silently disable a limit. Secrets (live account ID)
come from the environment, never from the committed file.
"""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path("data/config/trading.json")
ACCOUNT_ID_ENV = "TRADING_ACCOUNT_ID"


class Mode(str, Enum):
    PAPER = "paper"
    SHADOW = "shadow"
    APPROVE = "approve"
    AUTO = "auto"


class MarginType(str, Enum):
    REG_T = "reg_t"
    PORTFOLIO = "portfolio"


class StrategyName(str, Enum):
    COVERED_CALL = "covered_call"
    INDEX_PUT = "index_put"
    STOCK_PUT = "stock_put"
    PUT_SPREAD = "put_spread"
    TAIL_HEDGE = "tail_hedge"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_range(value: Tuple[float, float]) -> Tuple[float, float]:
    low, high = value
    if not 0.0 <= low <= high <= 1.0:
        raise ValueError(f"range must satisfy 0 <= low <= high <= 1, got {value}")
    return value


class StrategySwitch(_Strict):
    manage: bool = True
    open: bool = False


class WatchlistEntry(_Strict):
    strategies: List[StrategyName] = Field(default_factory=list)
    cc_max_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    cc_delta: Optional[Tuple[float, float]] = None
    call_roll_delta: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    min_oi: Optional[int] = Field(default=None, ge=0)
    max_spread_pct: Optional[float] = Field(default=None, gt=0.0, le=1.0)
    fair_value_proxy: Optional[str] = None
    order_step_s: Optional[int] = Field(default=None, gt=0)
    sector: Optional[str] = None  # for the per-sector cap; ETFs: "index"
    beta: Optional[float] = Field(default=None, gt=0.0, le=5.0)  # vs. SPY; default 1.0

    @field_validator("cc_delta")
    @classmethod
    def _cc_delta_range(cls, v):
        return v if v is None else _check_range(v)


class IndexConfig(_Strict):
    underlyings: List[str] = Field(default_factory=lambda: ["XSP", "SPXW"])
    settlement: str = "PM"
    switch_to_spx_at_contracts: int = Field(default=10, gt=0)

    @field_validator("settlement")
    @classmethod
    def _pm_only(cls, v):
        if v != "PM":
            raise ValueError("only PM-settled index options are allowed (spec §5.2)")
        return v


class EntryConfig(_Strict):
    dte_min: int = Field(default=30, gt=0)
    dte_max: int = Field(default=45, gt=0)
    cc_dte_min: int = Field(default=21, gt=0)
    cc_delta: Tuple[float, float] = (0.08, 0.15)
    index_put_delta: Tuple[float, float] = (0.10, 0.16)
    stock_put_delta: Tuple[float, float] = (0.10, 0.20)
    cc_max_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    cc_skip_strong_uptrend: bool = True
    min_vol_edge: float = Field(default=1.10, ge=1.0)
    ivr_min_index: float = Field(default=25, ge=0, le=100)
    ivr_min_stock: float = Field(default=30, ge=0, le=100)
    min_net_credit_per_contract: float = Field(default=20, ge=0)

    @field_validator("cc_delta", "index_put_delta", "stock_put_delta")
    @classmethod
    def _delta_ranges(cls, v):
        return _check_range(v)

    @model_validator(mode="after")
    def _dte_order(self):
        if self.dte_min > self.dte_max or self.cc_dte_min > self.dte_max:
            raise ValueError("dte_min and cc_dte_min must be <= dte_max")
        return self


class ReplacementTest(_Strict):
    min_captured: float = Field(default=0.5, ge=0.0, le=1.0)
    rel_efficiency: float = Field(default=0.5, ge=0.0, le=1.0)
    max_fee_ratio: float = Field(default=0.10, ge=0.0, le=1.0)


class GammaExit(_Strict):
    dte: int = Field(default=10, ge=0)
    max_remaining_extrinsic: float = Field(default=0.20, ge=0.0, le=1.0)
    min_delta: float = Field(default=0.25, ge=0.0, le=1.0)


class ExitConfig(_Strict):
    hold_to_expiration: bool = True
    replacement_test: ReplacementTest = ReplacementTest()
    gamma_exit: GammaExit = GammaExit()
    never_buy_back_below: float = Field(default=0.05, ge=0.0)


class GuardConfig(_Strict):
    put_roll_delta: float = Field(default=0.35, gt=0.0, le=1.0)
    call_roll_delta: float = Field(default=0.40, gt=0.0, le=1.0)
    min_extrinsic: float = Field(default=0.15, ge=0.0)
    pin_dte: int = Field(default=5, ge=0)
    pin_pct: float = Field(default=0.02, ge=0.0, le=1.0)
    earnings_delta: float = Field(default=0.25, ge=0.0, le=1.0)
    roll_min_credit: float = Field(default=0.05, ge=0.0)
    roll_max_dte: int = Field(default=90, gt=0)
    max_rolls: int = Field(default=4, ge=0)
    chain_max_loss_x_credit: float = Field(default=3.0, gt=0.0)
    on_put_assignment: str = "sell_shares"
    on_call_assignment: str = "rebuy_shares"

    @field_validator("on_put_assignment")
    @classmethod
    def _put_action(cls, v):
        if v not in {"sell_shares", "keep"}:
            raise ValueError("on_put_assignment must be 'sell_shares' or 'keep'")
        return v

    @field_validator("on_call_assignment")
    @classmethod
    def _call_action(cls, v):
        if v not in {"rebuy_shares", "keep_cash"}:
            raise ValueError("on_call_assignment must be 'rebuy_shares' or 'keep_cash'")
        return v


class MarginFloorConfig(_Strict):
    excess_liquidity_min: float = Field(default=100_000, gt=0)
    entry_buffer: float = Field(default=25_000, ge=0)
    restore_target: float = Field(default=110_000, gt=0)
    emergency_level: float = Field(default=50_000, ge=0)
    cushion_min_new_trade: float = Field(default=0.40, ge=0.0, le=1.0)
    cushion_breach: float = Field(default=0.30, ge=0.0, le=1.0)
    cushion_restore: float = Field(default=0.35, ge=0.0, le=1.0)
    cushion_emergency: float = Field(default=0.15, ge=0.0, le=1.0)
    check_interval_s: int = Field(default=60, gt=0)
    auto: bool = True

    @model_validator(mode="after")
    def _ordering(self):
        floor = self.excess_liquidity_min
        if not self.emergency_level < floor <= self.restore_target:
            raise ValueError(
                "need emergency_level < excess_liquidity_min <= restore_target"
            )
        if not (
            self.cushion_emergency
            < self.cushion_breach
            <= self.cushion_restore
            <= self.cushion_min_new_trade
        ):
            raise ValueError(
                "need cushion_emergency < cushion_breach <= cushion_restore "
                "<= cushion_min_new_trade"
            )
        return self

    @property
    def entry_min_excess_liquidity(self) -> float:
        """EL required after a new trade: the R1 floor plus the entry buffer."""
        return self.excess_liquidity_min + self.entry_buffer


class RiskConfig(_Strict):
    risk_budget_pct_nlv: float = Field(default=0.10, gt=0.0, le=1.0)
    short_put_notional_max: float = Field(default=0.50, ge=0.0, le=2.0)
    index_put_notional_max: float = Field(default=0.40, ge=0.0, le=2.0)
    per_name_max: float = Field(default=0.15, gt=0.0, le=1.0)
    per_sector_max: float = Field(default=0.35, gt=0.0, le=1.0)
    expiry_max_share: float = Field(default=0.35, gt=0.0, le=1.0)
    options_beta_delta_max: float = Field(default=0.35, ge=0.0, le=2.0)
    stress_margin_multiplier: float = Field(default=1.5, ge=1.0)
    single_name_gap: float = Field(default=0.35, gt=0.0, lt=1.0)
    single_name_gap_max_loss: float = Field(default=0.02, gt=0.0, le=1.0)
    options_daily_loss_halt: float = Field(default=0.015, gt=0.0, le=1.0)
    options_dd_reduce: float = Field(default=0.05, gt=0.0, le=1.0)
    options_dd_halt: float = Field(default=0.08, gt=0.0, le=1.0)
    options_dd_recover: float = Field(default=0.02, ge=0.0, le=1.0)
    max_orders_per_day: int = Field(default=5, ge=0)
    max_contracts_per_order: int = Field(default=10, gt=0)
    max_contracts_spx: int = Field(default=5, gt=0)
    max_contracts_xsp: int = Field(default=20, gt=0)

    @model_validator(mode="after")
    def _ordering(self):
        if self.index_put_notional_max > self.short_put_notional_max:
            raise ValueError("index_put_notional_max must be <= short_put_notional_max")
        if self.options_dd_reduce >= self.options_dd_halt:
            raise ValueError("options_dd_reduce must be < options_dd_halt")
        return self


class RegimeMultipliers(_Strict):
    low: float = Field(default=0.5, ge=0.0, le=1.0)
    normal: float = Field(default=0.8, ge=0.0, le=1.0)
    elevated: float = Field(default=1.0, ge=0.0, le=1.0)
    rising: float = Field(default=0.0, ge=0.0, le=1.0)
    fading: float = Field(default=0.6, ge=0.0, le=1.0)


class RegimeConfig(_Strict):
    vix_low: float = Field(default=13, gt=0)
    vix_mid: float = Field(default=20, gt=0)
    vix_high: float = Field(default=30, gt=0)
    fade_from_high: float = Field(default=0.15, gt=0.0, lt=1.0)
    mult: RegimeMultipliers = RegimeMultipliers()

    @model_validator(mode="after")
    def _ordering(self):
        if not self.vix_low < self.vix_mid < self.vix_high:
            raise ValueError("need vix_low < vix_mid < vix_high")
        return self


class TailHedgeConfig(_Strict):
    underlying: str = "SPY"
    otm: Tuple[float, float] = (0.10, 0.15)
    dte: Tuple[int, int] = (60, 120)
    budget_pct_premium: Tuple[float, float] = (0.05, 0.10)
    roll_dte: int = Field(default=30, gt=0)
    take_profit_x: float = Field(default=3.0, gt=1.0)

    @field_validator("otm", "budget_pct_premium")
    @classmethod
    def _fraction_ranges(cls, v):
        return _check_range(v)

    @field_validator("dte")
    @classmethod
    def _dte_range(cls, v):
        if not 0 < v[0] <= v[1]:
            raise ValueError(f"dte range must satisfy 0 < low <= high, got {v}")
        return v


class FeesConfig(_Strict):
    per_contract: float = Field(default=0.65, ge=0.0)
    tier: str = "ibkr_pro_tiered"
    margin_rate_source: str = "ib_benchmark"


def _default_strategies() -> Dict[StrategyName, StrategySwitch]:
    return {name: StrategySwitch() for name in StrategyName}


class TradingConfig(_Strict):
    mode: Mode = Mode.PAPER
    account_id: Optional[str] = None
    margin_type: MarginType = MarginType.REG_T
    enabled_strategies: Dict[StrategyName, StrategySwitch] = Field(
        default_factory=_default_strategies
    )
    watchlist: Dict[str, WatchlistEntry] = Field(default_factory=dict)
    index: IndexConfig = IndexConfig()
    entry: EntryConfig = EntryConfig()
    exit: ExitConfig = ExitConfig()
    guard: GuardConfig = GuardConfig()
    margin_floor: MarginFloorConfig = MarginFloorConfig()
    risk: RiskConfig = RiskConfig()
    regime: RegimeConfig = RegimeConfig()
    tail_hedge: TailHedgeConfig = TailHedgeConfig()
    fees: FeesConfig = FeesConfig()

    @field_validator("watchlist")
    @classmethod
    def _upper_symbols(cls, v):
        return {symbol.upper(): entry for symbol, entry in v.items()}

    @field_validator("enabled_strategies")
    @classmethod
    def _fill_missing_strategies(cls, v):
        """Strategies not listed get the safe default (manage only, never open)."""
        return {**_default_strategies(), **v}

    def strategy(self, name: StrategyName) -> StrategySwitch:
        # pylint cannot infer pydantic field types; this is a dict at runtime.
        return self.enabled_strategies[name]  # pylint: disable=unsubscriptable-object


def load_trading_config(
    path: Path | str = DEFAULT_CONFIG_PATH, env: Optional[Dict[str, str]] = None
) -> TradingConfig:
    """Load and validate the trading config.

    A missing file returns defaults (paper mode, nothing opens). An invalid
    file raises ``pydantic.ValidationError``; the engine must not start on a
    config it can't trust. The account ID from the environment
    (``TRADING_ACCOUNT_ID``) overrides the file.
    """
    env = os.environ if env is None else env
    path = Path(path)
    data = json.loads(path.read_text()) if path.exists() else {}
    account_id = env.get(ACCOUNT_ID_ENV)
    if account_id:
        data["account_id"] = account_id
    return TradingConfig.model_validate(data)
