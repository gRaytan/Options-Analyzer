"""Regression tests for the validation review (fixes PR V1).

One test (or group) per finding, named after it, so each fix stays fixed.
"""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.base import BrokerError
from src.trading.broker.fake import FakeAdapter
from src.trading.broker.ib_adapter import IBAdapter, IBConnectionSettings
from src.trading.calendars import Dividend, EventCalendar, SymbolEvents
from src.trading.config import MarginFloorConfig, MarginType, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Fill,
    Greeks,
    HaltScope,
    HaltState,
    IntentKind,
    MarginImpact,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Quote,
    Right,
    StockContract,
    TradeIntent,
)
from src.trading.journal import Journal
from src.trading.kill_switch import HaltManager
from src.trading.margin_monitor import MarginMonitor
from src.trading.reconcile import expected_from_fills, reconcile
from src.trading.risk.limits import LimitContext, exposures_from, gate_decision
from src.trading.risk.margin_type import classify_margin_type
from src.trading.risk.sanity import OrderContext, sanity_checks
from src.trading.status import MarginState

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
XSP = OptionContract(
    "XSP",
    date(2026, 12, 31),
    600,
    Right.PUT,
    style=ExerciseStyle.EUROPEAN,
    cash_settled=True,
)
SPY_HEDGE = OptionContract("SPY", date(2027, 2, 19), 580, Right.PUT)
AAPL_PUT = OptionContract("AAPL", date(2026, 11, 20), 200, Right.PUT)


def quote(c, bid=1.0, ask=1.1, delta=None, und=None):
    return Quote(
        c, bid, ask, NOW, underlying_price=und, greeks=Greeks(delta) if delta else None
    )


# -- Finding 1: a "close" must reduce a held position ---------------------------


def close_intent(qty, contract=XSP, action=Action.SELL):
    return TradeIntent(
        "c1", "x", IntentKind.CLOSE, (OrderLeg(contract, action, qty),), "T", NOW, True
    )


def sanity(intent, positions, halt=HaltState(True, HaltScope.OPENING, "x")):
    order = OrderRequest(intent.intent_id, intent.legs, -1.05, intent.intent_id)
    ctx = OrderContext(NOW, {XSP.key: quote(XSP)}, positions, halt=halt)
    return {c.name for c in sanity_checks(order, intent, ctx).failures}


def test_f1_fake_close_cannot_open_risk_during_halt():
    # Selling 5 puts we don't hold, labeled as a close: must be rejected.
    assert "reduces_position" in sanity(close_intent(5), {})


def test_f1_close_in_same_direction_rejected():
    assert "reduces_position" in sanity(close_intent(1), {XSP.key: -2})


def test_f1_real_close_passes_during_opening_halt():
    real = close_intent(2, action=Action.BUY)
    order = OrderRequest("c1", real.legs, 1.05, "c1")
    ctx = OrderContext(
        NOW,
        {XSP.key: quote(XSP)},
        {XSP.key: -2},
        halt=HaltState(True, HaltScope.OPENING, "x"),
    )
    d = sanity_checks(order, real, ctx)
    assert d.approved, d.summary()


# -- Finding 2: unknown delta must not hide risk --------------------------------


def test_f2_unquoted_hedge_does_not_offset_short_puts():
    holdings = [Position(XSP, -20, 3.0), Position(SPY_HEDGE, 10, 2.0)]
    quotes = {XSP.key: quote(XSP, delta=-0.14, und=660)}  # hedge has no quote
    exp = exposures_from(holdings, quotes)
    hedge = [e for e in exp if e.contract == SPY_HEDGE][0]
    assert hedge.delta == 0.0  # no credit for an unquoted long put
    acct = AccountSnapshot("DU1", NOW, 1_000_000, 500_000, 3e5, 3.8e5, 4e5)
    ctx = LimitContext.from_config(
        TradingConfig(),
        account=acct,
        impact=MarginImpact(8e5, 3.8e5, 3e5),
        exposures=exp,
        quotes=quotes,
    )
    more = TradeIntent(
        "o", "x", IntentKind.OPEN, (OrderLeg(XSP, Action.SELL, 20),), "T", NOW
    )
    failed = {c.name for c in gate_decision(more, ctx, TradingConfig()).failures}
    assert "options_beta_delta" in failed


# -- Finding 3: what-if can't hang ----------------------------------------------


def test_f3_what_if_times_out(monkeypatch):
    from src.trading.broker import ib_adapter
    from tests.trading.test_quotes import QuoteIB

    class HangingIB(QuoteIB):
        async def whatIfOrderAsync(self, contract, order):
            await asyncio.sleep(3600)  # IB error 321 leaves the request pending

    monkeypatch.setattr(ib_adapter, "WHAT_IF_TIMEOUT_S", 0.05)

    async def no_sleep(_):
        return None

    ib = HangingIB()
    a = IBAdapter("DU123", ib_factory=lambda: ib, sleep=no_sleep)
    order = OrderRequest("r", (OrderLeg(XSP, Action.SELL, 1),), -1.0, "r")

    async def go():
        await a.connect()
        await a.what_if(order)

    with pytest.raises(BrokerError, match="Read-Only API"):
        asyncio.run(go())


# -- Finding 4: assignment at expiry is not "expired" -------------------------------


def test_f4_assignment_at_expiry_detected():
    exp = expected_from_fills([Fill("e", "r", AAPL_PUT, Action.SELL, 2, 3.0, NOW)])
    previous = {AAPL_PUT.key: -2, "AAPL": 0}
    now = [Position(StockContract("AAPL"), 200, 200.0)]
    result = reconcile(exp, now, date(2026, 11, 23), previous)
    # Detected (finding 4) and, since T28c, explained as an assignment to handle.
    assert not result.should_halt
    [a] = result.assignments
    assert a.option_key == AAPL_PUT.key and a.contracts == 2 and a.shares == 200


def test_f4_worthless_expiry_still_explained():
    exp = expected_from_fills([Fill("e", "r", AAPL_PUT, Action.SELL, 2, 3.0, NOW)])
    previous = {AAPL_PUT.key: -2}
    result = reconcile(exp, [], date(2026, 11, 23), previous)
    assert result.ok and result.expired == (AAPL_PUT.key,)


def test_f4_cash_settled_expiry_never_assignment():
    old = OptionContract(
        "XSP",
        date(2026, 11, 20),
        600,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
    )
    exp = expected_from_fills([Fill("e", "r", old, Action.SELL, 1, 3.0, NOW)])
    assert reconcile(exp, [], date(2026, 11, 23), {old.key: -1}).ok


# -- Finding 5 + restart: the breach latch ----------------------------------------


def snap(el, ts=NOW):
    return AccountSnapshot("DU1", ts, 200_000, el, 1e5, 1e5, 1e5)


def test_f5_no_data_reading_keeps_breach():
    m = MarginMonitor(MarginFloorConfig(), lambda: NOW)
    assert m.update(snap(95_000)).state is MarginState.BREACH
    assert m.update(None).state is MarginState.BREACH
    assert m.update(snap(105_000)).state is MarginState.BREACH  # below $110k restore


def test_restart_during_breach_stays_breach(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    broker = FakeAdapter(account_id="DU123", read_only=True)

    def once(el, nlv):
        broker.set_account(excess_liquidity=el, net_liquidation=nlv)
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "HALT",
            )
        )
        return json.loads((tmp_path / "hb.json").read_text())["margin"]["state"]

    assert once(280_000, 1_000_000) == "breach"  # 28%
    assert once(320_000, 1_000_000) == "breach"  # new process, 32%: still breach
    assert once(360_000, 1_000_000) == "warning"  # restored (>= 35%)


# -- Finding 8: repeating errors don't grow the halt reason ---------------------


def test_f8_repeated_error_recorded_once(tmp_path):
    j = Journal(tmp_path / "j.db")
    halts = HaltManager(j, tmp_path / "HALT")
    for _ in range(5):
        run._trigger_once(halts, "engine error: RuntimeError: bug")
    assert j.count("halts") == 1


# -- Finding 9: margin type uses IB's own change figure --------------------------


def test_f9_margin_type_prefers_ib_change():
    put = OptionContract("SPY", date(2026, 12, 31), 600, Right.PUT)
    stale_account = AccountSnapshot(
        "DU1", NOW, 1e6, 5e5, 3e5, 375_000, 4e5
    )  # moved since
    impact = MarginImpact(8e5, 386_550, 3e5, init_margin_change=6_550)
    result = classify_margin_type(stale_account, impact, put, 670, 1.50)
    assert result.measured == 6_550 and result.detected is MarginType.REG_T


# -- Finding 10: engine reconnects when the adapter gave up ---------------------


def test_f10_engine_reconnects_after_adapter_gave_up(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    broker = FakeAdapter(account_id="DU123", read_only=True)
    halts = HaltManager(Journal(tmp_path / "j.db"), tmp_path / "HALT")

    async def go():
        await broker.connect()
        broker.drop_connection()  # and no reconnect loop running
        await run._ensure_connected(broker, halts)

    asyncio.run(go())
    assert broker.is_connected


# -- Finding 11: Docker relay ports ----------------------------------------------


def test_f11_docker_paper_port_is_paper():
    a = IBAdapter(
        "DU123", IBConnectionSettings(port=4004), ib_factory=lambda: _NullIB()
    )
    assert not a.is_live
    b = IBAdapter(
        "DU123", IBConnectionSettings(port=4003), ib_factory=lambda: _NullIB()
    )
    assert b.is_live


class _NullIB:
    def __init__(self):
        from eventkit import Event

        self.disconnectedEvent = Event("d")
        self.accountValueEvent = Event("a")


# -- Finding 12: buying stock adds exposure --------------------------------------


def test_f12_stock_buy_has_exposure():
    from src.trading.risk.limits import apply_intent

    aapl = StockContract("AAPL")
    buy = TradeIntent(
        "b", "x", IntentKind.OPEN, (OrderLeg(aapl, Action.BUY, 1000),), "T", NOW
    )
    after = apply_intent([], buy, {aapl.key: quote(aapl, 229.9, 230.1)})
    assert after[0].delta_dollars == pytest.approx(230_000)


# -- Finding 13: unknown ex-dividend is not "no dividend" ------------------------


def events_with(ev, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "o.json"
    p.write_text("{}")
    return EventCalendar(lambda s: ev, overrides_path=p, cache_path=tmp_path / "c.json")


def test_f13_unknown_ex_dividend_is_none(tmp_path):
    cal = events_with(SymbolEvents(dividend=None, dividend_known=False), tmp_path)
    assert cal.ex_dividend_risk("AAPL", date(2026, 12, 31), date(2026, 11, 23)) is None


def test_f13_known_dividend_and_known_none(tmp_path):
    div = SymbolEvents(dividend=Dividend(date(2026, 12, 10), 0.26), dividend_known=True)
    assert (
        events_with(div, tmp_path).ex_dividend_risk(
            "AAPL", date(2026, 12, 31), date(2026, 11, 23)
        )
        is True
    )
    none = SymbolEvents(dividend=None, dividend_known=True)
    assert (
        events_with(none, tmp_path / "x").ex_dividend_risk(
            "TSLA", date(2026, 12, 31), date(2026, 11, 23)
        )
        is False
    )


# -- Finding 14: P&L baseline taken when IB refreshed ----------------------------


def test_f14_baseline_uses_pnl_at_account_update():
    m = MarginMonitor(MarginFloorConfig(), lambda: NOW)
    s = AccountSnapshot("DU1", NOW, 1_000_000, 420_000, 3e5, 3.8e5, 4e5)
    # IB refreshed when P&L was -1,000; the monitor first sees it at -41,000.
    r = m.update(s, daily_pnl=-41_000, pnl_at_snapshot=-1_000)
    assert r.pnl_since_refresh == -40_000


# -- Engine heartbeat while IB is unreachable ------------------------------------


def test_heartbeat_written_while_disconnected(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))

    class Unreachable(FakeAdapter):
        async def connect(self):
            raise BrokerError("cannot connect to IB")

    broker = Unreachable(account_id="DU123", read_only=True)

    async def go():
        task = asyncio.create_task(
            run.monitor(
                0,
                adapter=broker,
                journal=Journal(tmp_path / "j.db"),
                halt_file=tmp_path / "H",
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.01)
            if hb.exists():
                break
        task.cancel()

    asyncio.run(go())
    beat = json.loads(hb.read_text())
    assert beat["connected"] is False and "cannot connect" in beat["connect_error"]
