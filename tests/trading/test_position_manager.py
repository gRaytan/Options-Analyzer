"""Position manager (plan T25, spec §5.8)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.config import TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    OptionContract,
    OrderRequest,
    Position,
    Quote,
    Right,
)
from src.trading.position_manager import Candidate, PositionManager, efficiency
from src.trading.risk.sanity import OrderContext, sanity_checks

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
ACCOUNT = AccountSnapshot("DU1", NOW, 1_000_000, 700_000, 300_000, 300_000, 650_000)
CFG = TradingConfig()


def put(dte=30, strike=600):
    return OptionContract(
        "XSP",
        TODAY + timedelta(days=dte),
        strike,
        Right.PUT,
        style=ExerciseStyle.EUROPEAN,
        cash_settled=True,
    )


def setup_one(contract, credit, bid, ask, und=660, delta=-0.10, qty=-2, iv=0.18):
    pos = Position(contract, qty, credit, (bid + ask) / 2)
    q = Quote(
        contract, bid, ask, NOW, underlying_price=und, greeks=Greeks(delta, iv=iv)
    )
    return [pos], {contract.key: q}, {contract.key}


def review(positions, quotes, managed, candidate=None):
    return PositionManager(CFG).review(
        positions, quotes, ACCOUNT, managed, TODAY, candidate, NOW
    )


GOOD = Candidate(efficiency=6e-4, credit=150.0)


def test_hold_by_default():
    assert (
        review(*setup_one(put(), credit=1.00, bid=0.58, ask=0.62)) == []
    )  # 40% captured
    assert (
        review(*setup_one(put(), credit=1.00, bid=0.18, ask=0.22)) == []
    )  # no candidate


def test_stop_loss_at_3x_credit_loss():
    [d] = review(
        *setup_one(put(), credit=1.00, bid=3.95, ask=4.05, und=580, delta=-0.7)
    )
    assert d.rule == "stop_loss" and d.intent.trigger == "STOP_LOSS"
    assert (
        review(*setup_one(put(), credit=1.00, bid=3.85, ask=3.95, und=580, delta=-0.7))
        == []
    )


def test_never_buy_back_cheap_options():
    assert review(*setup_one(put(), credit=1.00, bid=0.03, ask=0.05), GOOD) == []


def test_gamma_exit():
    # 6 DTE, put 2 ITM: intrinsic 2.00, mid 2.15 -> time value 0.15 < 20% of 1.00 credit.
    c = put(dte=6)
    [d] = review(*setup_one(c, credit=1.00, bid=2.10, ask=2.20, und=598, delta=-0.6))
    assert d.rule == "gamma_exit"
    assert (
        review(
            *setup_one(
                put(dte=12), credit=1.00, bid=2.10, ask=2.20, und=598, delta=-0.6
            )
        )
        == []
    )
    assert (
        review(*setup_one(c, credit=1.00, bid=2.10, ask=2.20, und=598, delta=-0.2))
        == []
    )


def test_replacement_when_new_trade_is_much_better_and_fees_small():
    positions, quotes, managed = setup_one(
        put(), credit=1.00, bid=0.18, ask=0.22
    )  # 80% captured
    [d] = review(positions, quotes, managed, GOOD)
    assert d.rule == "replacement" and d.intent.details["captured"] == pytest.approx(
        0.8
    )


def test_no_replacement_if_fees_eat_the_new_credit():
    positions, quotes, managed = setup_one(put(), credit=1.00, bid=0.18, ask=0.22)
    assert review(positions, quotes, managed, Candidate(6e-4, credit=15.0)) == []


def test_no_replacement_if_not_enough_captured_or_candidate_not_better():
    assert review(*setup_one(put(), credit=1.00, bid=0.58, ask=0.62), GOOD) == []
    positions, quotes, managed = setup_one(put(), credit=1.00, bid=0.18, ask=0.22)
    assert review(positions, quotes, managed, Candidate(1e-6, 150.0)) == []


def test_skips_unmanaged_long_and_unquoted():
    positions, quotes, _ = setup_one(put(), credit=1.00, bid=3.95, ask=4.05, und=580)
    assert review(positions, quotes, set()) == []  # the owner's position
    long_hedge = [Position(put(), 2, 1.0)]
    assert review(long_hedge, quotes, {put().key}) == []
    assert review(positions, {}, {put().key}) == []


def test_close_intent_shape_passes_the_close_check():
    positions, quotes, managed = setup_one(
        put(), credit=1.00, bid=3.95, ask=4.05, und=580
    )
    [d] = review(positions, quotes, managed)
    it = d.intent
    assert it.kind is IntentKind.CLOSE and it.risk_reducing
    assert it.legs[0].action is Action.BUY and it.legs[0].quantity == 2
    again = review(positions, quotes, managed)[0].intent
    assert again.intent_id == it.intent_id  # deterministic for the day
    order = OrderRequest(it.intent_id, it.legs, 4.00, it.intent_id)
    ctx = OrderContext(NOW, quotes, {put().key: -2})
    assert sanity_checks(order, it, ctx).approved


def test_efficiency_units():
    assert efficiency(1.50, 100, 35, 7_000) == pytest.approx(150 / 35 / 7_000)
    assert efficiency(1.0, 100, 0, 1) == 0 and efficiency(1.0, 100, 10, 0) == 0
