"""Daily opportunities from real data: report scan, digest, schedule, page."""
import asyncio
import math
from datetime import date, datetime, time, timedelta

from src.trading import run
from src.trading import scheduler as sched
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import StrategyName, TradingConfig
from src.trading.domain import (
    Bar,
    HistoryKind,
    OptionContract,
    StockContract,
    set_clock,
)
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.scanner import report_strategies
from tests.trading.test_entries import EXP, NOW, TODAY, _ivs, xsp
from tests.trading.test_scheduler import simulate


def market(broker):
    from tests.trading.test_scanner import fair_quote, series

    bars = series(650, 0.008)
    last = bars[-1].close
    broker.set_history("XSP", HistoryKind.PRICE, bars)
    broker.set_history(
        "XSP",
        HistoryKind.IMPLIED_VOL,
        [
            Bar(TODAY - timedelta(days=252 - i), v, v, v, v)
            for i, v in enumerate(_ivs())
        ],
    )
    broker.set_history(
        "VIX",
        HistoryKind.PRICE,
        [
            Bar(TODAY - timedelta(days=6 - i), v, v, v, v)
            for i, v in enumerate([22, 23, 24, 25, 24, 23])
        ],
    )
    base = round(last / 10) * 10
    strikes = tuple(float(base - 10 * i) for i in range(1, 12))
    broker.set_chain("XSP", [OptionChain("XSP", "XSP", 100, (EXP,), strikes)])
    for k in strikes:
        m = math.log(last / k)
        delta = -0.5 * math.erfc(m / (0.22 * math.sqrt(38 / 365)) / math.sqrt(2))
        broker.set_quote(fair_quote(xsp(k), last, 0.22, delta, width=0.02))


def test_report_strategies():
    s = report_strategies(TradingConfig())
    assert {
        StrategyName.COVERED_CALL,
        StrategyName.INDEX_PUT,
        StrategyName.PUT_SPREAD,
    } <= s
    assert StrategyName.STOCK_PUT not in s and StrategyName.TAIL_HEDGE not in s
    cfg = TradingConfig.model_validate(
        {"watchlist": {"AAPL": {"strategies": ["stock_put"]}}}
    )
    assert StrategyName.STOCK_PUT in report_strategies(cfg)


def test_engine_reports_opportunities_with_nothing_enabled(tmp_path, monkeypatch):
    """Default config (no strategy may open): the engine still scans the real market
    and journals opportunities, and orders nothing."""
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_position(StockContract("SPY"), 400, 670.0)
    market(broker)
    # The chain's expiry is EXP, 38 days after the fixture's TODAY; pin the
    # engine clock so the DTE window still matches whenever this runs.
    set_clock(lambda: NOW)
    try:
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
                config=TradingConfig(),
            )
        )
    finally:
        set_clock(None)
    scan = Journal(tmp_path / "j.db").latest("scan_results").data
    strategies = {c["strategy"] for c in scan["best"]}
    assert "index_put" in strategies
    assert set(scan["report_only"]) >= {"index_put", "put_spread", "covered_call"}
    assert scan["regime"] == "elevated"
    asyncio.run(broker.connect())
    assert not [
        p
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, OptionContract)
    ]
    assert Journal(tmp_path / "j.db").read("orders") == []


class Notes:
    def __init__(self):
        self.sent = []

    def notify(self, level, key, title, message):
        self.sent.append((level, key, title, message))


def test_digest_lists_the_best(tmp_path):
    j = Journal(tmp_path / "j.db")
    now = datetime(2026, 11, 23, 10, 0, tzinfo=sched.ET)
    set_clock(lambda: now)
    try:
        n = Notes()
        run._opportunities_digest(
            j, TradingConfig(), n
        )  # pylint: disable=protected-access
        assert "No scan yet today" in n.sent[0][3]
        j.append(
            "scan_results",
            {
                "date": "2026-11-23",
                "regime": "fading",
                "report_only": ["index_put"],
                "rejected": {},
                "best": [
                    {
                        "strategy": "put_spread",
                        "key": "XSP 2026-12-31 P 600",
                        "fire": True,
                        "delta": -0.09,
                        "net_credit": 61.0,
                        "annual_yield": 0.42,
                    },
                    {
                        "strategy": "covered_call",
                        "key": "QQQM 2026-12-18 C 270",
                        "fire": False,
                        "delta": 0.1,
                        "net_credit": 35.0,
                        "annual_yield": 0.08,
                    },
                ],
            },
        )
        n = Notes()
        rep = run._opportunities_digest(
            j, TradingConfig(), n
        )  # pylint: disable=protected-access
    finally:
        set_clock(None)
    level, key, title, body = n.sent[0]
    assert (
        level == "report"
        and key == "opps:2026-11-23"
        and title == "Opportunities 2026-11-23"
    )
    assert "READY put spread: XSP 2026-12-31 P 600 (delta 0.09)" in body
    assert "fading" in body and "Report only" in body
    assert len(rep["best"]) == 2
    assert Journal(tmp_path / "j.db").latest("job_runs").data["job"] == "opportunities"


def test_digest_is_scheduled_once_at_ten():
    assert simulate(date(2026, 11, 23))[sched.OPPORTUNITIES] == [time(10, 0)]
    assert simulate(date(2026, 11, 27))[sched.OPPORTUNITIES] == [
        time(10, 0)
    ]  # early close
    assert sched.OPPORTUNITIES not in simulate(date(2026, 11, 26))  # Thanksgiving


def test_page_shows_regime_and_report_only(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    Journal(tmp_path / "j.db").append(
        "scan_results",
        {
            "date": "2026-11-23",
            "regime": "elevated",
            "report_only": ["index_put"],
            "rejected": {},
            "best": [
                {
                    "strategy": "index_put",
                    "key": "XSP 2026-12-31 P 600",
                    "contracts": 1,
                    "bid": 3.0,
                    "ask": 3.1,
                    "delta": -0.13,
                    "iv": 0.22,
                    "credit": 305.0,
                    "net_credit": 302.7,
                    "ev": 120.0,
                    "stress_loss": 7100.0,
                    "annual_yield": 0.21,
                    "score": 0.0019,
                    "raw_score": 0.0016,
                    "timing": 1.2,
                    "fire": True,
                    "timing_reasons": [],
                    "dte": 38,
                    "long_key": None,
                    "width": 0.0,
                },
            ],
        },
    )
    html = create_app().test_client().get("/bot/opportunities").get_data(as_text=True)
    assert (
        "VIX regime: <b>elevated</b>" in html
        and "not enabled to open" in html
        and ">report<" in html
    )
