"""Rebuild plan after a margin call: shares by tranche, owner approves each item."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from src.trading import rebuild, run
from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.broker.fake import FakeAdapter
from src.trading.config import Mode, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    IntentKind,
    OrderLeg,
    Position,
    Quote,
    StockContract,
    TradeIntent,
    utcnow,
)
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.mode_policy import Route, route
from src.trading.risk.limits import LimitContext, exposures_from, gate_decision
from src.trading.broker.base import MarginImpact

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
CFG = TradingConfig()
SPY, QQQM = StockContract("SPY"), StockContract("QQQM")
PRICES = {"SPY": 670.0, "QQQM": 250.0}


def account(nlv=1_000_000, el=700_000, maint=300_000, cash=0.0):
    return AccountSnapshot("DU1", NOW, nlv, el, maint, maint, nlv - maint, cash)


def held(spy=400):
    return [Position(SPY, spy, 600.0, PRICES["SPY"])]


def test_parse_targets():
    assert rebuild.parse_targets("SPY=50%, qqqm=50") == {"SPY": 0.5, "QQQM": 0.5}
    assert rebuild.parse_targets("SPY=0.6") == {"SPY": 0.6}
    for bad in ("", "SPY=80,QQQM=40", "S P Y=10", "SPY=abc"):
        with pytest.raises(ValueError):
            rebuild.parse_targets(bad)


def test_one_tranche_toward_each_target():
    steps, fit, notes = rebuild.plan_shares(
        held(), {}, account(), PRICES, {"SPY": 0.5, "QQQM": 0.5}, CFG, "elevated", TODAY
    )
    by = {s.symbol: s for s in steps}
    # Target $500k each; a quarter per step = $125k; SPY gap is $232k so $125k.
    assert by["SPY"].shares == int(125_000 // 670)
    assert by["QQQM"].shares == int(125_000 // 250)
    assert fit.scale == 1.0 and fit.binding == "" and not notes
    assert fit.stress_el_after >= 100_000 and fit.cushion_after >= 0.40
    assert fit.gross_after <= 1_000_000


def test_rising_vix_halves_and_targets_already_met():
    steps, _, notes = rebuild.plan_shares(
        held(spy=1000),
        {},
        account(),
        PRICES,
        {"SPY": 0.5, "QQQM": 0.5},
        CFG,
        "rising",
        TODAY,
    )
    assert [s.symbol for s in steps] == ["QQQM"]
    assert steps[0].shares == int(62_500 // 250)
    assert any("already at or above" in n for n in notes) and any(
        "rising" in n for n in notes
    )


def test_tranche_shrinks_to_fit_the_margin_floor():
    # Cushion 45%: buying $250k of stock adds $62.5k of maintenance -> 38.75%,
    # under the 40% new-trade floor, so the tranche is cut (to about 80%).
    tight = account(el=450_000, maint=550_000)
    steps, fit, notes = rebuild.plan_shares(
        held(), {}, tight, PRICES, {"SPY": 0.5, "QQQM": 0.5}, CFG, "elevated", TODAY
    )
    assert 0 < fit.scale < 1 and fit.binding
    f = CFG.margin_floor
    assert fit.el_after >= f.excess_liquidity_min + f.entry_buffer - 1
    assert fit.cushion_after >= f.cushion_min_new_trade - 1e-3
    assert any("cut to" in n for n in notes)


def test_no_room_means_no_buys():
    breach = account(el=90_000, maint=910_000)
    steps, fit, notes = rebuild.plan_shares(
        held(), {}, breach, PRICES, {"SPY": 0.6}, CFG, "elevated", TODAY
    )
    assert steps == [] and fit.scale == 0 and "Restore the margin cushion" in notes[-1]


def test_plan_items_always_ask_and_have_their_own_ids():
    steps, _, _ = rebuild.plan_shares(
        held(), {}, account(), PRICES, {"SPY": 0.5}, CFG, "elevated", TODAY
    )
    [it] = rebuild.stock_intents("p1", steps, CFG, NOW)
    assert it.legs[0].action is Action.BUY and it.details["rebuild"]
    assert it.details["worst_price"] == pytest.approx(670 * 1.003, abs=0.01)
    for mode in (Mode.PAPER, Mode.APPROVE, Mode.AUTO):
        cfg = CFG.model_copy(update={"mode": mode, "auto_open": True})
        assert route(it, cfg, is_live=False) is Route.ASK
    assert route(it, CFG.model_copy(update={"mode": Mode.SHADOW}), False) is Route.LOG
    opt = TradeIntent(
        "x",
        "covered_call",
        IntentKind.OPEN,
        (OrderLeg(SPY, Action.SELL, 1),),
        "SCAN_READY",
        NOW,
    )
    [o] = rebuild.option_intents("p1", [opt], CFG)
    assert (
        o.intent_id != "x" and o.details["require_approval"] and o.trigger == "REBUILD"
    )


def test_gate_lets_approved_share_buys_past_option_caps():
    steps, _, _ = rebuild.plan_shares(
        held(), {}, account(), PRICES, {"SPY": 0.5}, CFG, "elevated", TODAY
    )
    [it] = rebuild.stock_intents("p1", steps, CFG, NOW)
    ctx = LimitContext.from_config(
        CFG,
        account=account(),
        impact=MarginImpact(1_000_000, 350_000, 330_000),
        exposures=exposures_from(held(), {}),
        quotes={},
        opening_orders_today=9,
    )
    d = gate_decision(it, ctx, CFG)
    assert d.approved, d.summary()  # SPY > 15% NLV, 186 shares, 9 orders today: fine
    plain = TradeIntent("y", "other", IntentKind.OPEN, it.legs, "X", NOW)
    names = {c.name for c in gate_decision(plain, ctx, CFG).failures}
    assert {"per_name", "contracts_per_order", "orders_per_day"} <= names
    tight = LimitContext.from_config(
        CFG,
        account=account(),
        impact=MarginImpact(400_000, 350_000, 330_000),
        exposures=exposures_from(held(), {}),
        quotes={},
    )
    assert "el_after" in {c.name for c in gate_decision(it, tight, CFG).failures}


# -- the engine: request -> plan -> approve -> fill (paper) ------------------------------


def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_position(SPY, 400, 600.0)

    def quotes():
        for c, px in ((SPY, 670.0), (QQQM, 250.0)):
            broker.set_quote(Quote(c, px - 0.01, px + 0.01, utcnow(), last=px))

    async def no_sleep(_):
        return None

    def once():
        quotes()
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
                config=TradingConfig(),
                order_sleep=no_sleep,
            )
        )

    return broker, once


def shares(broker):
    asyncio.run(broker.connect())
    return {
        p.contract.symbol: p.quantity
        for p in asyncio.run(broker.positions())
        if isinstance(p.contract, StockContract)
    }


def test_engine_round_trip_waits_for_each_approval(tmp_path, monkeypatch):
    broker, once = engine(tmp_path, monkeypatch)
    j = Journal(tmp_path / "j.db")
    j.append(
        "rebuild_requests",
        {"request_id": "r1", "targets": {"SPY": 0.5, "QQQM": 0.5}, "options": False},
    )
    once()
    j = Journal(tmp_path / "j.db")
    plan = j.latest("rebuild_plans").data
    assert plan["request_id"] == "r1" and len(plan["intents"]) == 2
    assert shares(broker) == {"SPY": 400}  # paper mode, but nothing sent yet
    q = ApprovalQueue(j)
    spy, qqqm = plan["intents"]
    assert q.status(spy.intent_id) is ApprovalStatus.PENDING
    q.approve(spy.intent_id, by="test")
    q.reject(qqqm.intent_id, by="test")
    once()
    assert shares(broker) == {
        "SPY": 400 + spy.legs[0].quantity
    }  # only the approved one
    once()  # nothing more: filled once, the rejected one never sent
    assert shares(broker) == {"SPY": 400 + spy.legs[0].quantity}
    assert len(Journal(tmp_path / "j.db").read("rebuild_plans")) == 1  # built once


def test_page_requests_a_plan_and_lists_items(tmp_path, monkeypatch):
    from src.web.app import create_app

    broker, once = engine(tmp_path, monkeypatch)
    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    c = create_app().test_client()
    html = c.get("/bot/rebuild").get_data(as_text=True)
    assert "Build plan" in html
    with c.session_transaction() as s:
        s["_csrf"] = "t"
    resp = c.post(
        "/bot/rebuild",
        data={"csrf_token": "t", "targets": "SPY=50%, QQQM=50%", "options": "on"},
    )
    assert resp.status_code == 302
    req = Journal(tmp_path / "j.db").latest("rebuild_requests").data
    assert req["targets"] == {"SPY": 0.5, "QQQM": 0.5} and req["options"] is True
    assert "Plan requested" in c.get("/bot/rebuild").get_data(as_text=True)
    once()
    html = c.get("/bot/rebuild").get_data(as_text=True)
    assert "Buy shares" in html and "waiting for you" in html and "Approve" in html
    plan = Journal(tmp_path / "j.db").latest("rebuild_plans").data
    iid = plan["intents"][0].intent_id
    resp = c.post(
        f"/bot/approvals/{iid}",
        data={"csrf_token": "t", "decision": "approve", "next": "/bot/rebuild"},
    )
    assert resp.headers["Location"].endswith("/bot/rebuild")
    bad = c.post(
        f"/bot/approvals/{iid}",
        data={"csrf_token": "t", "decision": "approve", "next": "https://evil.example"},
    )
    assert bad.headers["Location"].endswith("/bot/approvals")


def test_engine_plan_with_options_runs_the_scanner(tmp_path, monkeypatch):
    broker, once = engine(tmp_path, monkeypatch)
    Journal(tmp_path / "j.db").append(
        "rebuild_requests",
        {"request_id": "r2", "targets": {"SPY": 0.6}, "options": True},
    )
    once()
    plan = Journal(tmp_path / "j.db").latest("rebuild_plans").data
    assert [i.strategy for i in plan["intents"]] == ["rebuild"]
    # No price/IV history in the fake broker: the scanner finds nothing, and says so.
    assert any("No option trade" in n or "Options skipped" in n for n in plan["notes"])
