"""Live positions -> Portfolio (plan T08)."""
import asyncio
from datetime import date

import pytest
from eventkit import Event
from ib_async.contract import Contract as IBContract
from ib_async.objects import PortfolioItem
from ib_async.objects import Position as IBPosition

from src.analysis.margin import calculate_portfolio_margin
from src.analysis.risk import analyze_portfolio_risk
from src.models.portfolio import Portfolio
from src.models.position import OptionPosition, OptionType, StockPosition
from src.trading.broker.base import BrokerDisconnected
from src.trading.broker.ib_adapter import IBAdapter, contract_from_ib
from src.trading.domain import (
    AccountSnapshot,
    ExerciseStyle,
    OptionContract,
    Position,
    Right,
    StockContract,
)
from src.trading.portfolio_state import build_portfolio, legacy_option_symbol

QQQM_STK = IBContract(secType="STK", symbol="QQQM", currency="USD")
QQQM_CALL = IBContract(
    secType="OPT",
    symbol="QQQM",
    lastTradeDateOrContractMonth="20261218",
    strike=240,
    right="C",
    multiplier="100",
    currency="USD",
    tradingClass="QQQM",
)
SPXW_PUT = IBContract(
    secType="OPT",
    symbol="SPX",
    lastTradeDateOrContractMonth="20261231",
    strike=5500,
    right="P",
    multiplier="100",
    currency="USD",
    tradingClass="SPXW",
)
FUTURE = IBContract(secType="FUT", symbol="ES", localSymbol="ESZ6")


class PositionsIB:
    def __init__(self, items=(), positions=()):
        self.items = list(items)
        self.plain = list(positions)
        self.connected = False
        self.disconnectedEvent = Event("d")
        self.accountValueEvent = Event("a")

    async def connectAsync(self, *a, **k):
        self.connected = True

    def reqMarketDataType(self, data_type):
        self.market_data_type = data_type

    def reqPnL(self, account, modelCode=""):
        from ib_async.objects import PnL

        self.pnl = PnL(account=account)
        return self.pnl

    def managedAccounts(self):
        return ["DU123"]

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False

    def accountValues(self, account=""):
        return []

    def portfolio(self, account=""):
        return self.items

    def positions(self, account=""):
        return self.plain


def adapter_with(ib):
    adapter = IBAdapter("DU123", ib_factory=lambda: ib)
    asyncio.run(adapter.connect())
    return adapter


def item(contract, qty, avg, price, account="DU123"):
    return PortfolioItem(contract, qty, price, qty * price, avg, 0.0, 0.0, account)


def test_contract_translation():
    assert contract_from_ib(QQQM_STK) == StockContract("QQQM")
    call = contract_from_ib(QQQM_CALL)
    assert call == OptionContract(
        "QQQM", date(2026, 12, 18), 240, Right.CALL, trading_class="QQQM"
    )
    assert call.can_be_assigned_early
    spxw = contract_from_ib(SPXW_PUT)
    assert spxw.style is ExerciseStyle.EUROPEAN and spxw.cash_settled
    assert spxw.key == "SPXW 2026-12-31 P 5500"
    assert contract_from_ib(FUTURE) is None


def test_positions_from_portfolio_items():
    ib = PositionsIB(
        items=[
            item(QQQM_STK, 300, 180.0, 210.0),
            item(QQQM_CALL, -1, 125.0, 0.80),  # IB avg cost is per contract
            item(SPXW_PUT, -2, 2400.0, 18.5),
            item(FUTURE, 1, 6000.0, 6010.0),
            item(QQQM_STK, 50, 1.0, 1.0, account="DU999"),
            item(QQQM_STK, 0, 0.0, 0.0),
        ]
    )
    adapter = adapter_with(ib)
    positions = asyncio.run(adapter.positions())
    by_key = {p.contract.key: p for p in positions}
    assert by_key["QQQM"] == Position(StockContract("QQQM"), 300, 180.0, 210.0)
    call = by_key["QQQM 2026-12-18 C 240"]
    assert call.quantity == -1 and call.avg_cost == pytest.approx(1.25)
    assert call.market_price == 0.80
    assert by_key["SPXW 2026-12-31 P 5500"].avg_cost == pytest.approx(24.0)
    assert len(positions) == 3
    assert adapter.unsupported_positions == ["FUT ESZ6"]


def test_positions_fallback_without_prices():
    ib = PositionsIB(positions=[IBPosition("DU123", QQQM_CALL, -1, 125.0)])
    [pos] = asyncio.run(adapter_with(ib).positions())
    assert pos.market_price is None and pos.avg_cost == pytest.approx(1.25)


def test_nan_market_price_becomes_none():
    ib = PositionsIB(items=[item(QQQM_STK, 100, 180.0, float("nan"))])
    [pos] = asyncio.run(adapter_with(ib).positions())
    assert pos.market_price is None


def test_positions_require_connection():
    adapter = IBAdapter("DU123", ib_factory=PositionsIB)
    with pytest.raises(BrokerDisconnected):
        asyncio.run(adapter.positions())


def test_legacy_symbol_matches_csv_format():
    opt = OptionContract("AAPL", date(2027, 1, 15), 110, Right.PUT)
    assert legacy_option_symbol(opt) == "AAPL 15JAN27 110 P"
    spxw = OptionContract(
        "SPX", date(2026, 12, 31), 5500, Right.PUT, trading_class="SPXW"
    )
    assert legacy_option_symbol(spxw) == "SPXW 31DEC26 5500 P"


def csv_equivalent_portfolio() -> Portfolio:
    """What the CSV parser produces for the same holdings."""
    p = Portfolio()
    p.stocks.append(StockPosition("QQQM", 300, 180.0, 210.0))
    p.options.append(
        OptionPosition(
            symbol="QQQM 18DEC26 240 C",
            underlying="QQQM",
            strike=240,
            expiration=date(2026, 12, 18),
            option_type=OptionType.CALL,
            quantity=-4,  # 300 shares cover 3; 1 is naked
            cost_price=1.25,
            current_price=0.80,
            underlying_price=210.0,
        )
    )
    p.options.append(
        OptionPosition(
            symbol="AAPL 15JAN27 200 P",
            underlying="AAPL",
            strike=200,
            expiration=date(2027, 1, 15),
            option_type=OptionType.PUT,
            quantity=-2,
            cost_price=3.10,
            current_price=9.40,
            underlying_price=190.0,
        )
    )
    return p


def test_same_analysis_as_csv_portfolio():
    live = build_portfolio(
        [
            Position(StockContract("QQQM"), 300, 180.0, 210.0),
            Position(
                OptionContract("QQQM", date(2026, 12, 18), 240, Right.CALL),
                -4,
                1.25,
                0.80,
            ),
            Position(
                OptionContract("AAPL", date(2027, 1, 15), 200, Right.PUT),
                -2,
                3.10,
                9.40,
            ),
        ],
        underlying_prices={"AAPL": 190.0},
    )
    csv = csv_equivalent_portfolio()
    assert calculate_portfolio_margin(live) == pytest.approx(
        calculate_portfolio_margin(csv)
    )
    for a, b in zip(live.options, csv.options):
        assert a.symbol == b.symbol
        assert a.margin_requirement == pytest.approx(b.margin_requirement)
        assert a.unrealized_pnl == pytest.approx(b.unrealized_pnl)
    live_risk, csv_risk = analyze_portfolio_risk(live), analyze_portfolio_risk(csv)
    assert live_risk.summary == csv_risk.summary
    assert [o.flags for o in live.options] == [o.flags for o in csv.options]


def test_account_fields_copied():
    acct = AccountSnapshot(
        "DU123", None, 1_000_000, 450_000, 300_000, 380_000, 420_000, -5000
    )
    p = build_portfolio([], acct)
    assert p.account_id == "DU123"
    assert p.net_asset_value == p.net_liquidation_value == 1_000_000
    assert p.excess_liquidity == 450_000
    assert p.cash_balance == -5000


def test_missing_market_price_falls_back_to_cost():
    p = build_portfolio([Position(StockContract("QQQ"), 10, 500.0)])
    assert p.stocks[0].current_price == 500.0
