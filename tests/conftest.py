"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_trading_cache(tmp_path, monkeypatch):
    """Keep history caches out of the repo's data/ folder."""
    monkeypatch.setenv("TRADING_CACHE_DIR", str(tmp_path / "trading-cache"))
