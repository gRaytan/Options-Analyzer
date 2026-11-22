"""Tests for the trading config schema (plan T02, spec §10)."""
import json

import pytest
from pydantic import ValidationError

from src.trading.config import (
    ACCOUNT_ID_ENV,
    MarginType,
    Mode,
    StrategyName,
    TradingConfig,
    load_trading_config,
)


def write(tmp_path, data):
    path = tmp_path / "trading.json"
    path.write_text(json.dumps(data))
    return path


def test_missing_file_gives_safe_defaults(tmp_path):
    cfg = load_trading_config(tmp_path / "nope.json", env={})
    assert cfg.mode is Mode.PAPER
    assert cfg.margin_type is MarginType.REG_T
    assert cfg.account_id is None
    # Nothing opens by default; everything is managed.
    for name in StrategyName:
        assert cfg.strategy(name).open is False
        assert cfg.strategy(name).manage is True


def test_defaults_match_spec():
    cfg = TradingConfig()
    assert cfg.margin_floor.excess_liquidity_min == 100_000
    assert cfg.margin_floor.entry_min_excess_liquidity == 125_000
    assert cfg.margin_floor.cushion_min_new_trade == 0.40
    assert cfg.margin_floor.cushion_breach == 0.30
    assert cfg.risk.risk_budget_pct_nlv == 0.10
    assert cfg.risk.short_put_notional_max == 0.50
    assert cfg.guard.put_roll_delta == 0.35
    assert cfg.guard.call_roll_delta == 0.40
    assert cfg.entry.cc_delta == (0.08, 0.15)
    assert cfg.index.settlement == "PM"


def test_committed_config_is_valid():
    cfg = load_trading_config(env={})
    assert cfg.mode is Mode.PAPER
    qqqm = cfg.watchlist["QQQM"]
    assert qqqm.fair_value_proxy == "QQQ"
    assert qqqm.cc_delta == (0.06, 0.10)
    assert cfg.strategy(StrategyName.STOCK_PUT).open is False


def test_unknown_keys_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_trading_config(write(tmp_path, {"mod": "auto"}), env={})
    with pytest.raises(ValidationError):
        load_trading_config(
            write(tmp_path, {"margin_floor": {"excess_liquidty_min": 1}}), env={}
        )


@pytest.mark.parametrize(
    "data",
    [
        {"mode": "yolo"},
        {"margin_type": "cash"},
        {"entry": {"cc_delta": [0.2, 0.1]}},
        {"entry": {"index_put_delta": [0.1, 1.5]}},
        {"entry": {"dte_min": 50, "dte_max": 45}},
        {"index": {"settlement": "AM"}},
        {"margin_floor": {"restore_target": 90_000}},
        {"margin_floor": {"cushion_breach": 0.5}},
        {"risk": {"index_put_notional_max": 0.6, "short_put_notional_max": 0.5}},
        {"risk": {"options_dd_reduce": 0.1, "options_dd_halt": 0.08}},
        {"regime": {"vix_low": 25, "vix_mid": 20}},
        {"guard": {"on_put_assignment": "take_shares"}},
        {"watchlist": {"QQQM": {"strategies": ["naked_call"]}}},
        {"watchlist": {"QQQM": {"cc_max_coverage": 1.5}}},
    ],
)
def test_invalid_values_rejected(tmp_path, data):
    with pytest.raises(ValidationError):
        load_trading_config(write(tmp_path, data), env={})


def test_env_account_id_overrides_file(tmp_path):
    path = write(tmp_path, {"account_id": "DU000"})
    cfg = load_trading_config(path, env={ACCOUNT_ID_ENV: "DU999"})
    assert cfg.account_id == "DU999"
    assert load_trading_config(path, env={}).account_id == "DU000"


def test_partial_strategy_list_fills_safe_defaults(tmp_path):
    path = write(
        tmp_path, {"enabled_strategies": {"index_put": {"manage": True, "open": True}}}
    )
    cfg = load_trading_config(path, env={})
    assert cfg.strategy(StrategyName.INDEX_PUT).open is True
    assert cfg.strategy(StrategyName.COVERED_CALL).open is False


def test_watchlist_symbols_uppercased(tmp_path):
    cfg = load_trading_config(write(tmp_path, {"watchlist": {"qqqm": {}}}), env={})
    assert "QQQM" in cfg.watchlist


def test_config_is_immutable():
    cfg = TradingConfig()
    with pytest.raises(ValidationError):
        cfg.mode = Mode.AUTO
