"""De-risk plan: naked calls, then short puts by exposure removed per $, then penny calls."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import derisk, run
from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.broker.fake import FakeAdapter
from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
    utcnow,
)
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.mode_policy import Route, route

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
CFG = TradingConfig()
EXP = date(2027, 6, 17)
ACCT = AccountSnapshot(
    "U1", NOW, 300_000, 120_000, 180_000, 200_000, 100_000
)  # cap: $150k of puts


def opt(und, k, right=Right.PUT, exp=EXP):
    return OptionContract(und, exp, k, right)


UND = {
    "QQQ": 630.0,
    "GOOG": 283.0,
    "NBIS": 74.0,
    "META": 596.0,
    "NFLX": 59.0,
    "XSP": 718.0,
}


def q(c, mid, spread=0.10):
    return Quote(
        c,
        round(mid - spread / 2, 2),
        round(mid + spread / 2, 2),
        utcnow(),
        underlying_price=UND[c.underlying],
    )


def book():
    return [
        Position(StockContract("QQQ"), 30, 600.0, 630.0),
        Position(StockContract("NFLX"), 100, 50.0, 59.0),
        Position(
            opt("QQQ", 545), -2, 3.0, 1.5
        ),  # $109k for ~$300: cheap per $ of exposure
        Position(opt("GOOG", 230), -2, 6.0, 7.0),  # $46k for ~$1.4k
        Position(opt("NBIS", 70), -3, 9.0, 16.0),  # $21k for ~$4.8k: expensive per $
        Position(opt("QQQ", 755, Right.CALL), -1, 4.0, 4.8),  # naked: 30 shares only
        Position(
            opt("NFLX", 95, Right.CALL, date(2026, 12, 18)), -1, 0.5, 0.02
        ),  # penny, covered
        Position(
            opt("META", 1070, Right.CALL), -1, 3.0, 2.5
        ),  # covered at the other broker
    ]


def quotes():
    return {
        p.contract.key: q(p.contract, p.market_price)
        for p in book()
        if isinstance(p.contract, OptionContract)
    }


def test_plan_order_and_sizes():
    p = derisk.plan(
        book(), quotes(), ACCT, CFG, "r", NOW, external_shares={"META": 1000}
    )
    kinds = [(i.kind, i.position.contract.underlying, i.optional) for i in p.items]
    assert kinds[:2] == [("naked_call", "QQQ", False), ("naked_call", "META", True)]
    puts = [i for i in p.items if i.kind == "put"]
    assert [i.position.contract.underlying for i in puts] == [
        "QQQ"
    ]  # $176k -> $67k: under the cap
    assert p.put_exposure_before == pytest.approx(109_000 + 46_000 + 21_000)
    assert (
        p.put_exposure_after == pytest.approx(67_000)
        and p.put_exposure_after <= p.put_cap
    )
    assert ("penny_call", "NFLX", True) in kinds
    assert (
        "other broker"
        in [i for i in p.items if i.position.contract.underlying == "META"][0].reason
    )
    # Cash needed counts required items only; the account value drops by the spread only.
    assert p.cash_needed == pytest.approx(
        sum(i.cost for i in p.items if not i.optional)
    )
    assert 0 < p.friction < p.cash_needed


def test_most_exposure_per_dollar_first():
    tight = AccountSnapshot(
        "U1", NOW, 60_000, 30_000, 30_000, 30_000, 30_000
    )  # cap $30k
    p = derisk.plan(book(), quotes(), tight, CFG, "r", NOW)
    order = [i.position.contract.underlying for i in p.items if i.kind == "put"]
    assert order == ["QQQ", "GOOG"]  # NBIS (least exposure per $) stays: $21k <= $30k


def test_spread_short_leg_not_closed_alone():
    short, long_ = opt("XSP", 650), opt("XSP", 640)
    pos = [Position(short, -10, 3.0, 2.0), Position(long_, 10, 2.0, 1.5)]
    small = AccountSnapshot(
        "U1", NOW, 10_000, 5_000, 5_000, 5_000, 5_000
    )  # cap $5k < $10k spread
    p = derisk.plan(pos, {}, small, CFG, "r", NOW)
    assert [i.position.contract.key for i in p.items] == [
        short.key
    ]  # removes the $10k width


def test_statement_marks_assume_a_spread():
    c = opt("GOOG", 230)
    flat = {c.key: Quote(c, 7.0, 7.0, NOW)}  # statement: bid = ask = mark
    price, mark = derisk._prices(
        Position(c, -1, 6.0, 7.0), flat
    )  # pylint: disable=protected-access
    assert mark == 7.0 and price == pytest.approx(7.35)


def test_intents_ask_once_and_are_risk_reducing():
    p = derisk.plan(book(), quotes(), ACCT, CFG, "r", NOW)
    its = derisk.intents(p, CFG, NOW.date())
    assert len(its) == len(p.items) and len({i.intent_id for i in its}) == len(its)
    for it in its:
        assert it.risk_reducing and it.legs[0].action is Action.BUY
        assert it.details["require_approval"] and it.details["ask_once"]
        assert route(it, CFG, False) is Route.ASK
        assert it.details["worst_price"] > 0


def test_engine_builds_on_request_and_buys_only_approved(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    monkeypatch.setenv("TRADING_EXTERNAL_HOLDINGS", str(tmp_path / "none.json"))
    broker = FakeAdapter(account_id="U1", read_only=False, account=ACCT)
    broker.fill_threshold = 0.3
    for p in book():
        broker.set_position(p.contract, p.quantity, p.avg_cost)
    for c, qt in quotes().items():
        broker.set_quote(qt)

    async def no_sleep(_):
        return None

    def once():
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
                config=CFG,
                order_sleep=no_sleep,
            )
        )

    Journal(tmp_path / "j.db").append(
        "derisk_requests", {"request_id": "d1", "penny_calls": False}
    )
    once()
    j = Journal(tmp_path / "j.db")
    plan = j.latest("derisk_plans").data
    assert plan["request_id"] == "d1" and plan["put_exposure_after"] <= plan["put_cap"]
    qqq_put = [
        i for i in plan["intents"] if i.legs[0].contract.key == opt("QQQ", 545).key
    ][0]
    q_ = ApprovalQueue(j)
    assert q_.status(qqq_put.intent_id) is ApprovalStatus.PENDING
    q_.approve(qqq_put.intent_id, by="test")
    once()
    asyncio.run(broker.connect())
    held = {p.contract.key: p.quantity for p in asyncio.run(broker.positions())}
    assert opt("QQQ", 545).key not in held  # bought back
    assert (
        held[opt("GOOG", 230).key] == -2 and held[opt("QQQ", 755, Right.CALL).key] == -1
    )  # not approved
    assert len(Journal(tmp_path / "j.db").read("derisk_plans")) == 1


def test_pages(tmp_path, monkeypatch):
    from src.web.app import create_app
    from tests.trading.test_statement_check import portfolio

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    app = create_app()
    c = app.test_client()
    assert "Build de-risk plan" in c.get("/bot/derisk").get_data(as_text=True)
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    c.post("/bot/derisk", data={"csrf_token": "t", "penny_calls": "on"})
    assert (
        Journal(tmp_path / "j.db").latest("derisk_requests").data["penny_calls"] is True
    )
    small = portfolio()
    small.net_liquidation_value = 400_000.0  # $300k of short puts > the $200k limit
    app.portfolios = {"main": small}
    html = app.test_client().get("/bot/check").get_data(as_text=True)
    assert "De-risk plan (from the statement)" in html
