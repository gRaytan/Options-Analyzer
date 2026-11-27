"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_trading_cache(tmp_path, monkeypatch):
    """Keep history caches out of the repo's data/ folder."""
    monkeypatch.setenv("TRADING_CACHE_DIR", str(tmp_path / "trading-cache"))


@pytest.fixture(autouse=True)
def _real_clock_after_each_test():
    """A test that simulates time (domain.set_clock) never leaks it."""
    yield
    from src.trading.domain import set_clock

    set_clock(None)


@pytest.fixture(autouse=True)
def _default_margin_floor(monkeypatch):
    """Engine tests use the default margin floors, not the account's profile
    in data/config/trading.json (which can be tuned to the account's state)."""
    from src.trading import config as cfg
    from src.trading import run

    real = cfg.load_trading_config

    def load(*a, **k):
        return real(*a, **k).model_copy(
            update={"margin_floor": cfg.MarginFloorConfig()}
        )

    monkeypatch.setattr(run, "load_trading_config", load)
