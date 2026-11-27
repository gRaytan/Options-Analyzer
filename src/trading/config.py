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
    # IB's maintenance margin on the shares, when above Reg-T's 25% (volatile names).
    maint_margin: Optional[float] = Field(default=None, ge=0.25, le=1.0)

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
    # Sell covered calls on every stock held, not only watchlist names (at the
    # defaults here). Keeps the holdings list out of the committed config.
    cc_all_holdings: bool = False
    cc_skip_strong_uptrend: bool = True
    min_vol_edge: float = Field(default=1.10, ge=1.0)
    ivr_min_index: float = Field(default=25, ge=0, le=100)
    ivr_min_stock: float = Field(default=30, ge=0, le=100)
    min_net_credit_per_contract: float = Field(default=20, ge=0)
    min_fee_multiple: float = Field(
        default=5.0, ge=1.0
    )  # net credit >= 5x round-trip fees
    cc_strike_above_cost: bool = True  # covered-call strike >= the shares' cost basis
    max_spread_pct: float = Field(default=0.10, gt=0.0, le=1.0)
    max_spread_cheap: float = Field(default=0.10, gt=0.0)  # $ cap when mid < $1
    min_open_interest: int = Field(default=500, ge=0)  # stocks; index uses 100
    index_scan_root: str = "XSP"  # index puts are scanned on this root
    # Index put credit spreads (spec §5.4): used instead of naked index puts
    # in the spreads-only VIX regime or when Cushion is below spread_cushion_below.
    spread_short_delta: Tuple[float, float] = (0.10, 0.16)
    spread_fading_delta: Tuple[float, float] = (0.05, 0.10)  # VIX spike fading
    spread_max_loss_nlv: float = Field(default=0.01, gt=0.0, le=0.05)  # per position
    # Credit / width is about the short delta, so 20% (spec v0.4) is unreachable at
    # delta <= 0.16; 8% keeps out tiny credits, the EV test does the edge filtering.
    spread_min_credit_width: float = Field(default=0.08, gt=0.0, lt=1.0)
    spread_cushion_below: float = Field(default=0.50, ge=0.0, le=1.0)
    spread_max_width: float = Field(default=25.0, gt=0.0)  # $ of strike (XSP)

    @field_validator(
        "cc_delta",
        "index_put_delta",
        "stock_put_delta",
        "spread_short_delta",
        "spread_fading_delta",
    )
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


class RebuildConfig(_Strict):
    """Rebuilding positions after a margin call (plan R1, rebuild.py).

    Shares are bought back in ``tranches`` steps toward the targets, each
    sized so the crash scenario still leaves Excess Liquidity above the floor;
    total stock is capped at ``max_gross_leverage`` x NLV (1.0 = no borrowing).
    """

    tranches: int = Field(default=4, ge=1, le=20)
    max_gross_leverage: float = Field(default=1.0, gt=0.0, le=2.0)
    rising_vix_scale: float = Field(default=0.5, ge=0.0, le=1.0)  # VIX > 30 and rising
    worst_price_pct: float = Field(default=0.003, ge=0.0, le=0.05)  # stock limit cap
    approval_hours: float = Field(default=6.5, gt=0.0, le=72.0)  # plan items stay open
    max_option_items: int = Field(default=3, ge=0, le=10)


class RiskConfig(_Strict):
    risk_budget_pct_nlv: float = Field(default=0.10, gt=0.0, le=1.0)
    short_put_notional_max: float = Field(default=0.50, ge=0.0, le=2.0)
    index_put_notional_max: float = Field(default=0.40, ge=0.0, le=2.0)
    per_name_max: float = Field(default=0.15, gt=0.0, le=1.0)
    per_sector_max: float = Field(default=0.35, gt=0.0, le=1.0)
    expiry_max_share: float = Field(default=0.35, gt=0.0, le=1.0)
    options_beta_delta_max: float = Field(default=0.35, ge=0.0, le=2.0)
    stress_margin_multiplier: float = Field(default=1.5, ge=1.0)
    # The crash the account must survive (spec §6.2): S&P 500 down this much,
    # each name moving by its beta (at least 1x on the way down), IV +50% and
    # margin x stress_margin_multiplier; Excess Liquidity must stay >= the floor.
    crash_move: float = Field(default=-0.30, ge=-0.90, le=-0.05)
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
    """Crash hedge (hedge.py): a collar by default.

    Long XSP puts ``put_otm`` below the index, ``put_dte`` days out, sized to
    ``coverage`` x the beta-weighted stock exposure; paid for by calls
    ``call_otm`` above on ``call_coverage`` of the shares in ``call_symbols``
    (``style: "puts"`` buys the puts only). Rolled at ``roll_dte``. Every
    order waits for the owner's approval.
    """

    style: str = "collar"  # collar | puts
    put_root: str = "XSP"
    put_otm: float = Field(default=0.10, gt=0.0, lt=0.5)
    put_dte: Tuple[int, int] = (120, 240)
    coverage: float = Field(default=1.0, gt=0.0, le=1.5)
    call_otm: float = Field(default=0.08, gt=0.0, lt=0.5)
    call_coverage: float = Field(default=0.5, ge=0.0, le=1.0)
    call_symbols: List[str] = Field(default_factory=lambda: ["SPY", "QQQM", "QQQ"])
    roll_dte: int = Field(default=60, gt=0)
    min_shortfall: float = Field(default=0.10, ge=0.0, le=1.0)  # of the target
    max_debit_pct_nlv: float = Field(default=0.02, gt=0.0, le=0.2)  # per plan

    @field_validator("style")
    @classmethod
    def _style(cls, v):
        if v not in ("collar", "puts"):
            raise ValueError("style must be collar or puts")
        return v

    @field_validator("put_dte")
    @classmethod
    def _dte_range(cls, v):
        if not 0 < v[0] <= v[1]:
            raise ValueError(f"dte range must satisfy 0 < low <= high, got {v}")
        return v


class FeesConfig(_Strict):
    """Fee assumptions (T21). Approximate defaults; calibrated from real fills.

    IBKR Pro tiered US options commission by premium (verify on IB's site):
    < $0.05: $0.25; $0.05-0.10: $0.50; >= $0.10: $0.65 per contract, $1 minimum
    per order. Exchange and regulatory fees vary; these are estimates.
    """

    per_contract: float = Field(default=0.65, ge=0.0)
    per_contract_cheap: float = Field(default=0.50, ge=0.0)  # premium $0.05-0.10
    per_contract_tiny: float = Field(default=0.25, ge=0.0)  # premium < $0.05
    min_per_order: float = Field(default=1.00, ge=0.0)
    exchange_per_contract: float = Field(default=0.20, ge=0.0)  # equity/ETF options
    index_exchange_per_contract: Dict[str, float] = Field(
        default_factory=lambda: {"SPX": 0.60, "SPXW": 0.60, "XSP": 0.10}
    )
    regulatory_per_contract: float = Field(default=0.05, ge=0.0)  # ORF + OCC + TAF
    margin_rate_annual: float = Field(default=0.06, ge=0.0, le=0.5)  # on negative cash
    calibration_min_fills: int = Field(default=10, ge=1)
    tier: str = "ibkr_pro_tiered"
    margin_rate_source: str = "ib_benchmark"


class AlertsConfig(_Strict):
    """Alert routing (T20). Credentials come from the environment, never here."""

    min_level: str = "warning"  # info | warning | critical
    repeat_minutes: int = Field(default=15, ge=0)  # same alert at most this often
    max_per_hour: int = Field(default=30, gt=0)

    @field_validator("min_level")
    @classmethod
    def _level(cls, v):
        if v not in {"info", "warning", "critical"}:
            raise ValueError("min_level must be info, warning or critical")
        return v


def _default_strategies() -> Dict[StrategyName, StrategySwitch]:
    return {name: StrategySwitch() for name in StrategyName}


class TradingConfig(_Strict):
    mode: Mode = Mode.PAPER
    # Stage 5: entries go out without approval. Off = entries need approval
    # even in auto mode (Stage 4, "auto-manage"; spec §11).
    auto_open: bool = False
    # In auto mode, openings of these strategies go out without approval even
    # when auto_open is off (e.g. covered calls, which need no margin); the
    # rest still ask. The risk gate checks every order either way.
    auto_open_strategies: List[StrategyName] = Field(default_factory=list)
    # Key 1 of 2 for live trading; key 2 is TRADING_ALLOW_LIVE=yes in the
    # environment. Both are needed before any order can reach a live account.
    live_trading_enabled: bool = False
    account_id: Optional[str] = None
    margin_type: MarginType = MarginType.REG_T
    enabled_strategies: Dict[StrategyName, StrategySwitch] = Field(
        default_factory=_default_strategies
    )
    watchlist: Dict[str, WatchlistEntry] = Field(default_factory=dict)
    index: IndexConfig = IndexConfig()
    entry: EntryConfig = EntryConfig()
    rebuild: RebuildConfig = RebuildConfig()
    # Monthly option-income goal for the Metrics page ($; 0 = not shown).
    income_goal_monthly: float = Field(default=0.0, ge=0.0)
    exit: ExitConfig = ExitConfig()
    guard: GuardConfig = GuardConfig()
    margin_floor: MarginFloorConfig = MarginFloorConfig()
    risk: RiskConfig = RiskConfig()
    regime: RegimeConfig = RegimeConfig()
    tail_hedge: TailHedgeConfig = TailHedgeConfig()
    fees: FeesConfig = FeesConfig()
    alerts: AlertsConfig = AlertsConfig()

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
