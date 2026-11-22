"""Order manager (plan T22a, spec §7.1–7.3)."""
import asyncio
from datetime import date, datetime, timezone

import pytest

from src.trading.broker.fake import FakeAdapter
from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Quote,
    Right,
    TradeIntent,
)
from src.trading.journal import Journal
from src.trading.order_manager import (
    ExecStatus,
    OrderManager,
    ladder,
    round_to_tick,
    tick_size,
)
from src.trading.risk.checks import CheckResult, GateDecision

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT)
PUT2 = OptionContract("XSP", date(2027, 1, 29), 590, Right.PUT)
SPXW = OptionContract("SPX", date(2026, 12, 31), 6000, Right.PUT, trading_class="SPXW")


def test_ladder_steps_and_cap():
    # Selling at bid 1.00 / ask 1.10: best -1.10, natural -1.00.
    assert ladder(-1.10, -1.00) == pytest.approx([-1.05, -1.04, -1.03, -1.02, -1.01])
    assert ladder(-1.10, -1.00, emergency=True)[-1] == pytest.approx(-1.00)


def test_tick_and_rounding():
    assert tick_size(SPXW, 5.0) == 0.10 and tick_size(SPXW, 2.0) == 0.05
    assert tick_size(PUT, 5.0) == 0.01
    assert round_to_tick(-1.054, 0.01, -1.10, -1.01) == -1.05
    assert (
        round_to_tick(-1.006, 0.01, -1.10, -1.01) == -1.01
    )  # nearest would be -1.00 > cap
    assert round_to_tick(12.37, 0.10, 12.0, 12.36) == 12.30


class Clock:
    def __init__(self, broker, script=None):
        self.broker = broker
        self.script = script or {}  # sleep number -> callable
        self.n = 0

    async def __call__(self, _seconds):
        self.n += 1
        if self.n in self.script:
            self.script[self.n]()


def make_env(tmp_path, bid=1.00, ask=1.10, read_only=False):
    broker = FakeAdapter(account_id="DU1", read_only=read_only)
    asyncio.run(broker.connect())
    broker.set_quote(Quote(PUT, bid, ask, NOW))
    broker.set_quote(Quote(PUT2, 1.50, 1.62, NOW))
    journal = Journal(tmp_path / "j.db")
    return broker, journal


def intent(legs=None, iid="intent-1", kind=IntentKind.OPEN):
    legs = legs or (OrderLeg(PUT, Action.SELL, 2),)
    return TradeIntent(iid, "index_put", kind, tuple(legs), "TEST", NOW)


def quotes_from(broker):
    async def fn(contracts):
        return {c.key: await broker.quote(c) for c in contracts}

    return fn


async def approve(order, intent_, quotes):
    return GateDecision((CheckResult("test", True),))


async def reject(order, intent_, quotes):
    return GateDecision((CheckResult("limits", False, "too big"),))


def run(coro):
    return asyncio.run(coro)


def test_fills_when_market_comes_to_us_and_journals_everything(tmp_path):
    broker, journal = make_env(tmp_path)
    # After the 2nd wait the bid rises to 1.04, reaching our resting -1.04 limit.
    clock = Clock(broker, {2: lambda: broker.set_quote(Quote(PUT, 1.04, 1.08, NOW))})
    om = OrderManager(broker, journal, sleep=clock)
    res = run(om.execute(intent(), quotes_from(broker), approve))
    assert res.status is ExecStatus.FILLED and res.filled == 2
    assert res.prices[0] == -1.05
    assert journal.count("intents") == 1 and journal.count("orders") == 1
    assert journal.count("risk_checks") == 1 and journal.count("fills") == 1
    events = [r.data["event"] for r in journal.read("order_events")]
    assert events[0] == "placed" and "repriced" in events and events[-1] == "result"
    [pos] = run(broker.positions())
    assert pos.quantity == -2


def test_unfilled_after_ladder_is_cancelled(tmp_path):
    broker, journal = make_env(tmp_path)
    om = OrderManager(broker, journal, sleep=Clock(broker))
    res = run(om.execute(intent(), quotes_from(broker), approve))
    assert res.status is ExecStatus.UNFILLED
    assert res.prices == pytest.approx(
        [-1.05, -1.04, -1.03, -1.02, -1.01]
    )  # never past -1.01
    assert run(broker.open_orders()) == []
    assert om.failed_cycles["intent-1"] == 1


def test_skipped_for_the_day_after_three_failed_cycles(tmp_path):
    broker, journal = make_env(tmp_path)
    om = OrderManager(broker, journal, sleep=Clock(broker))
    for _ in range(3):
        run(om.execute(intent(), quotes_from(broker), approve))
    res = run(om.execute(intent(), quotes_from(broker), approve))
    assert res.status is ExecStatus.REFUSED and "skipped" in res.detail


def test_emergency_goes_to_natural(tmp_path):
    broker, journal = make_env(tmp_path)
    om = OrderManager(broker, journal, sleep=Clock(broker))
    res = run(om.execute(intent(), quotes_from(broker), approve, emergency=True))
    assert res.status is ExecStatus.FILLED and res.prices[-1] == -1.00


def test_gate_rejection_sends_nothing(tmp_path):
    broker, journal = make_env(tmp_path)
    om = OrderManager(broker, journal, sleep=Clock(broker))
    res = run(om.execute(intent(), quotes_from(broker), reject))
    assert res.status is ExecStatus.REJECTED and "too big" in res.detail
    assert journal.count("orders") == 0 and run(broker.open_orders()) == []
    assert journal.latest("risk_checks").data["approved"] is False


def test_refuses_read_only_and_live(tmp_path):
    broker, journal = make_env(tmp_path, read_only=True)
    om = OrderManager(broker, journal, sleep=Clock(broker))
    assert (
        run(om.execute(intent(), quotes_from(broker), approve)).status
        is ExecStatus.REFUSED
    )
    broker2, journal2 = make_env(tmp_path / "b")
    broker2.is_live = True
    om2 = OrderManager(broker2, journal2, sleep=Clock(broker2))
    res = run(om2.execute(intent(), quotes_from(broker2), approve))
    assert res.status is ExecStatus.REFUSED and "live" in res.detail


def test_idempotent_after_restart(tmp_path):
    broker, journal = make_env(tmp_path, bid=1.05, ask=1.06)  # tight: fills at once
    om = OrderManager(broker, journal, sleep=Clock(broker))
    assert (
        run(om.execute(intent(), quotes_from(broker), approve)).status
        is ExecStatus.FILLED
    )
    # A new manager (engine restart) with the same intent: nothing is sent again.
    om2 = OrderManager(broker, Journal(tmp_path / "j.db"), sleep=Clock(broker))
    res = run(om2.execute(intent(), quotes_from(broker), approve))
    assert res.status is ExecStatus.ALREADY_DONE
    assert run(broker.positions())[0].quantity == -2


def test_existing_working_order_not_duplicated(tmp_path):
    broker, journal = make_env(tmp_path)
    broker.auto_fill = False
    from src.trading.domain import OrderRequest

    run(broker.place_order(OrderRequest("intent-1", intent().legs, -1.08, "intent-1")))
    om = OrderManager(broker, journal, sleep=Clock(broker))
    res = run(om.execute(intent(), quotes_from(broker), approve))
    assert res.status is ExecStatus.ALREADY_DONE


def test_roll_as_one_combo(tmp_path):
    broker, journal = make_env(tmp_path, bid=1.00, ask=1.02)
    broker.set_position(PUT, -1, 1.0)
    roll = intent(
        (OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 1)),
        "roll-1",
        IntentKind.ROLL,
    )
    om = OrderManager(broker, journal, sleep=Clock(broker))
    res = run(om.execute(roll, quotes_from(broker), approve, emergency=True))
    assert res.status is ExecStatus.FILLED
    [pos] = run(broker.positions())
    assert pos.contract == PUT2 and pos.quantity == -1
    assert journal.count("fills") == 2  # one per leg


def test_step_override_for_qqqm(tmp_path):
    broker, journal = make_env(tmp_path)
    waits = []

    async def sleep(s):
        waits.append(s)

    qqqm = OptionContract("QQQM", date(2026, 12, 18), 270, Right.CALL)
    broker.set_quote(Quote(qqqm, 0.40, 0.60, NOW))
    om = OrderManager(broker, journal, sleep=sleep, step_overrides={"QQQM": 300})
    run(
        om.execute(
            intent((OrderLeg(qqqm, Action.SELL, 1),), "cc-1"),
            quotes_from(broker),
            approve,
        )
    )
    assert waits and set(waits) == {300}
