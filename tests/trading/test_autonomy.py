"""Autonomy: covered calls open on their own in auto mode; every order is reported."""
from src.trading import run
from src.trading.domain import Action, IntentKind, OrderLeg, OptionContract, Right
from src.trading.mode_policy import Route, route
from src.trading.order_manager import ExecStatus, ExecutionResult
from tests.trading.test_mode_policy import LATER, LIVE_ON, NOW, OPEN, YES, cfg, intent

CALL = OptionContract("SPY", LATER.expiry, 720, Right.CALL)
CC = intent(IntentKind.OPEN, "covered_call", legs=(OrderLeg(CALL, Action.SELL, 2),))


def test_auto_open_strategies():
    c = cfg("auto", **LIVE_ON, auto_open_strategies=["covered_call"])
    assert route(CC, c, True, YES) is Route.SEND  # covered call: on its own
    assert route(OPEN, c, True, YES) is Route.ASK  # index put: still asks
    plain = cfg("auto", **LIVE_ON)
    assert route(CC, plain, True, YES) is Route.ASK
    # Without the live keys nothing is sent, whatever the list says.
    assert route(CC, cfg("auto", auto_open_strategies=["covered_call"]), True, YES) is (
        Route.LOG
    )
    assert route(CC, c, True, {}) is Route.LOG


def test_order_result_messages():
    filled = ExecutionResult("i", ExecStatus.FILLED, 2, -1.85, prices=[-1.9, -1.85])
    title, body = run.order_result_message(CC, filled, approved=False)
    assert title == "Filled: covered call"
    assert "sell 2 SPY" in body and "$1.85" in body and "$370 premium in" in body
    unfilled = ExecutionResult("i", ExecStatus.UNFILLED, prices=[-1.9, -1.8])
    title, body = run.order_result_message(CC, unfilled, approved=False)
    assert title.startswith("Unfilled") and "1.90 to 1.80" in body
    rejected = ExecutionResult("i", ExecStatus.REJECTED, detail="stress_el failed")
    assert run.order_result_message(CC, rejected, approved=False) is None
    title, body = run.order_result_message(CC, rejected, approved=True)
    assert "rejected" in title and "stress_el" in body
    done = ExecutionResult("i", ExecStatus.ALREADY_DONE)
    assert run.order_result_message(CC, done, approved=False) is None
    buy = intent(IntentKind.CLOSE, legs=(OrderLeg(LATER, Action.BUY, 1),))
    paid = ExecutionResult("i", ExecStatus.FILLED, 1, 0.40)
    assert "$40 paid" in run.order_result_message(buy, paid, approved=False)[1]
