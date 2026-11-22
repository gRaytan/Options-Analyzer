"""Smoke tests: the package and its dependencies import cleanly."""
import importlib

import pytest


def test_trading_package_imports():
    trading = importlib.import_module("src.trading")
    assert trading.__version__


@pytest.mark.parametrize(
    "module",
    [
        "src.analysis.risk",
        "src.analysis.margin",
        "src.models.portfolio",
        "src.models.position",
    ],
)
def test_existing_analysis_modules_import(module):
    """The engine reuses these modules (spec G5), so they must stay importable."""
    importlib.import_module(module)


@pytest.mark.parametrize("module", ["ib_async", "pydantic", "exchange_calendars"])
def test_trading_dependencies_installed(module):
    importlib.import_module(module)
