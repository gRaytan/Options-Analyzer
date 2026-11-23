"""S&P -30% crash protection, and rebuilding BRK B / NBIS."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import rebuild, run
from src.trading.broker.base import OptionChain
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig, load_trading_config
from src.trading.domain import (
    AccountSnapshot,
    Bar,
    HistoryKind,
    Position,
    Right,
    StockContract,
    normalize_symbol,
)
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import StressModel, crash_scenario
from src.trading.scanner import candidate_contracts

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
REPO = load_trading_config("data/config/trading.json")


def acct(nlv=1_000_000, el=700_000, maint=300_000):
    return AccountSnapshot("DU1", NOW, nlv, el, maint, maint, nlv - maint, 0.0)


@pytest.mark.parametrize(
    "typed,ib",
    [
        ("BRK.B", "BRK B"),
        ("brk-b", "BRK B"),
        ("BRKB", "BRK B"),
        ("BRK B", "BRK B"),
        ("BRK/B", "BRK B"),
        ("nbis", "NBIS"),
        ("QQQM", "QQQM"),
    ],
)
def test_normalize_symbol(typed, ib):
    assert normalize_symbol(typed) == ib


def test_targets_accept_share_classes():
    assert rebuild.parse_targets("SPY=40%, QQQM=40%, BRK.B=10%, NBIS=5%") == {
        "SPY": 0.4,
        "QQQM": 0.4,
        "BRK B": 0.1,
        "NBIS": 0.05,
    }


def test_repo_config_covers_brkb_and_nbis_at_minus_30():
    assert REPO.risk.crash_move == -0.30
    assert (
        REPO.watchlist["NBIS"].beta == 2.5
        and REPO.watchlist["NBIS"].maint_margin == 0.40
    )
    assert REPO.watchlist["BRK B"].beta == 0.9
    assert crash_scenario(REPO).market_move == -0.30


def test_crash_moves_each_stock_by_its_beta():
    pos = [
        Position(StockContract("SPY"), 1000, 600.0, 600.0),
        Position(StockContract("NBIS"), 1000, 100.0, 100.0),
        Position(StockContract("BRK B"), 200, 500.0, 500.0),
    ]
    betas = {s: e.beta for s, e in REPO.watchlist.items() if e.beta}
    model = StressModel(exposures_from(pos, {}), {}, acct(), TODAY, betas)
    res = model.run(crash_scenario(REPO))
    # SPY -30% of $600k, NBIS -75% (2.5x) of $100k, BRK B -30% (beta floored at 1x) of $100k.
    assert res.stock_pnl == pytest.approx(-180_000 - 75_000 - 30_000)


def test_rebuild_sizes_nbis_with_its_beta_and_margin():
    held = [Position(StockContract("SPY"), 800, 600.0, 600.0)]
    prices = {"SPY": 600.0, "NBIS": 100.0}
    cfg = REPO
    steps, fit, notes = rebuild.plan_shares(
        held, {}, acct(), prices, {"SPY": 0.5, "NBIS": 0.3}, cfg, "elevated", TODAY
    )
    nbis = [s for s in steps if s.symbol == "NBIS"][0]
    assert nbis.shares == int(75_000 // 100)  # a quarter of the $300k target
    # Maintenance added: NBIS at 40%, not Reg-T's 25%.
    spy = [s for s in steps if s.symbol == "SPY"][0]
    assert fit.el_after == pytest.approx(700_000 - 0.25 * spy.cost - 0.40 * nbis.cost)
    assert any("NBIS: target 30%" in n and "75%" in n for n in notes)  # 2.5 x 30%
    assert not any(n.startswith("SPY: target") for n in notes)  # ETFs aren't flagged


def test_rebuild_stops_when_the_crash_would_breach():
    # $1.2M of NBIS on a $1M account: a -30% S&P (-75% NBIS) loses $900k and
    # leaves Excess Liquidity negative, so nothing more may be bought.
    held = [Position(StockContract("NBIS"), 12000, 100.0, 100.0)]
    steps, fit, notes = rebuild.plan_shares(
        held,
        {},
        acct(el=450_000, maint=550_000),
        {"NBIS": 100.0, "SPY": 600.0},
        {"SPY": 0.4},
        REPO,
        "elevated",
        TODAY,
    )
    assert steps == [] and "S&P crash" in fit.binding


def test_brkb_option_chain_matches_trading_class():
    chain = OptionChain(
        "BRK B",
        "BRKB",
        100,
        (TODAY + timedelta(days=30),),
        (480.0, 490.0, 510.0, 520.0),
    )
    calls = candidate_contracts(
        "BRK B", Right.CALL, [chain], 500, TODAY, 21, 45, "BRK B"
    )
    assert calls and {c.strike for c in calls} == {510.0, 520.0}


def test_engine_alerts_when_the_account_fails_the_crash_test(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    sent = []

    class Notes:
        def notify(self, level, key, title, message):
            sent.append((level, title, message))

        async def anotify(self, *a):
            self.notify(*a)

    # $1.5M of stock on a $1M account (borrowed): a -30% crash loses $450k+.
    broker = FakeAdapter(account_id="DU1", account=acct(el=200_000, maint=800_000))
    broker.set_position(StockContract("SPY"), 2500, 600.0)
    broker.set_history(
        "VIX",
        HistoryKind.PRICE,
        [Bar(TODAY - timedelta(days=5 - i), 20, 20, 20, 20) for i in range(6)],
    )
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
            notifier=Notes(),
            config=REPO,
        )
    )
    crash = [s for s in sent if "crash test" in s[1]]
    assert crash and crash[0][0] == "warning" and "-30%" in crash[0][1]
    import json

    hb = json.loads((tmp_path / "hb.json").read_text())
    assert (
        hb["budget"]["crash"]["survives"] is False
        and hb["budget"]["crash"]["move"] == -0.30
    )


def test_status_page_shows_the_crash_test(tmp_path, monkeypatch):
    from src.trading.status import write_heartbeat
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(tmp_path / "hb.json"))
    budget = {
        "regime": "elevated",
        "vix": 24,
        "multiplier": 1.0,
        "spreads_only": False,
        "budget": 100_000,
        "used": 20_000,
        "free": 80_000,
        "over_budget": False,
        "regime_detail": "VIX 24",
        "crash": {
            "move": -0.30,
            "stock_pnl": -300_000,
            "options_pnl": -40_000,
            "nlv_after": 660_000,
            "excess_liquidity_after": 210_000,
            "survives": True,
        },
    }
    write_heartbeat(tmp_path / "hb.json", connected=True, budget=budget)
    html = create_app().test_client().get("/bot").get_data(as_text=True)
    assert "If the S&amp;P 500 fell 30% today" in html and "no margin call" in html
