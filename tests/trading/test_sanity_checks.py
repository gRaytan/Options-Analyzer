"""Per-order sanity checks (plan T17, spec §6.3)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.broker.base import OrderState, OrderStatus
from src.trading.domain import (
    Action,
    HaltScope,
    HaltState,
    IntentKind,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Quote,
    Right,
    TradeIntent,
)
from src.trading.risk.sanity import (
    OrderContext,
    net_market,
    sanity_checks,
    working_contract_keys,
)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)
PUT2 = OptionContract("XSP", date(2027, 1, 29), 540, Right.PUT)


def q(contract, bid, ask, age=1, delayed=False):
    return Quote(contract, bid, ask, NOW - timedelta(seconds=age), delayed=delayed)


def intent(legs, kind=IntentKind.OPEN, risk_reducing=False, iid="i1"):
    return TradeIntent(iid, "index_put", kind, tuple(legs), "TEST", NOW, risk_reducing)


def order_for(it, limit):
    return OrderRequest(it.intent_id, it.legs, limit, it.intent_id)


def ctx(**kw):
    base = dict(
        now=NOW,
        quotes={PUT.key: q(PUT, 1.00, 1.10), PUT2.key: q(PUT2, 1.50, 1.62)},
        positions={},
    )
    base.update(kw)
    return OrderContext(**base)


SELL_PUT = intent([OrderLeg(PUT, Action.SELL, 2)])


def failed(decision):
    return {c.name for c in decision.failures}


def test_good_opening_order_passes():
    d = sanity_checks(order_for(SELL_PUT, -1.05), SELL_PUT, ctx())
    assert d.approved, d.summary()
    assert len(d.checks) == 6


@pytest.mark.parametrize(
    "limit,ok",
    [(-1.00, True), (-1.10, True), (-0.99, False), (-1.11, False)],
)
def test_single_leg_price_bounds(limit, ok):
    # Selling: natural = -bid (-1.00), best = -ask (-1.10).
    d = sanity_checks(order_for(SELL_PUT, limit), SELL_PUT, ctx())
    assert ("price_in_market" not in failed(d)) is ok


def test_combo_net_market_and_bounds():
    roll = intent(
        [OrderLeg(PUT, Action.BUY, 1), OrderLeg(PUT2, Action.SELL, 1)], IntentKind.ROLL
    )
    best, natural = net_market(order_for(roll, 0), ctx().quotes)
    assert best == pytest.approx(1.00 - 1.62) and natural == pytest.approx(1.10 - 1.50)
    assert sanity_checks(order_for(roll, -0.50), roll, ctx()).approved
    assert "price_in_market" in failed(
        sanity_checks(order_for(roll, -0.30), roll, ctx())
    )


@pytest.mark.parametrize(
    "quote,reason",
    [
        (None, "no quote"),
        (q(PUT, 1.0, 1.1, delayed=True), "delayed"),
        (q(PUT, 1.0, 1.1, age=6), "6s old"),
        (q(PUT, None, 1.1), "no two-sided"),
    ],
)
def test_quotes_must_be_live(quote, reason):
    quotes = {} if quote is None else {PUT.key: quote}
    d = sanity_checks(order_for(SELL_PUT, -1.05), SELL_PUT, ctx(quotes=quotes))
    [c] = [c for c in d.checks if c.name == "quotes_live"]
    assert not c.passed and reason in c.detail


def test_order_must_match_intent():
    other = OrderRequest("i1", (OrderLeg(PUT, Action.SELL, 3),), -1.05, "i1")
    assert "matches_intent" in failed(sanity_checks(other, SELL_PUT, ctx()))
    wrong_ref = OrderRequest("i1", SELL_PUT.legs, -1.05, "x")
    assert "matches_intent" in failed(sanity_checks(wrong_ref, SELL_PUT, ctx()))


def test_no_flip_on_close():
    close = intent([OrderLeg(PUT, Action.BUY, 3)], IntentKind.CLOSE, risk_reducing=True)
    d = sanity_checks(order_for(close, 1.05), close, ctx(positions={PUT.key: -2}))
    assert "no_flip" in failed(d)
    close2 = intent(
        [OrderLeg(PUT, Action.BUY, 2)], IntentKind.CLOSE, risk_reducing=True
    )
    assert sanity_checks(
        order_for(close2, 1.05), close2, ctx(positions={PUT.key: -2})
    ).approved


def test_adding_to_a_position_is_not_a_flip():
    d = sanity_checks(
        order_for(SELL_PUT, -1.05), SELL_PUT, ctx(positions={PUT.key: -5})
    )
    assert "no_flip" not in failed(d)


def test_duplicate_working_order():
    working = OrderState(order_for(SELL_PUT, -1.05), OrderStatus.WORKING)
    done = OrderState(order_for(SELL_PUT, -1.05), OrderStatus.FILLED)
    assert working_contract_keys([working, done]) == {PUT.key}
    assert working_contract_keys([done]) == set()
    d = sanity_checks(order_for(SELL_PUT, -1.05), SELL_PUT, ctx(working_keys={PUT.key}))
    assert "no_duplicate" in failed(d)


def test_opening_halt_blocks_opening_but_not_closing():
    halt = HaltState(True, HaltScope.OPENING, "daily loss")
    assert "halt" in failed(
        sanity_checks(order_for(SELL_PUT, -1.05), SELL_PUT, ctx(halt=halt))
    )
    close = intent([OrderLeg(PUT, Action.BUY, 2)], IntentKind.CLOSE, risk_reducing=True)
    d = sanity_checks(
        order_for(close, 1.05), close, ctx(halt=halt, positions={PUT.key: -2})
    )
    assert d.approved, d.summary()


def test_full_halt_blocks_everything():
    halt = HaltState(True, HaltScope.ALL, "reconcile")
    close = intent([OrderLeg(PUT, Action.BUY, 2)], IntentKind.CLOSE, risk_reducing=True)
    d = sanity_checks(
        order_for(close, 1.05), close, ctx(halt=halt, positions={PUT.key: -2})
    )
    assert "halt" in failed(d)


def test_all_failures_reported_and_summary():
    d = sanity_checks(
        order_for(SELL_PUT, -5.0),
        SELL_PUT,
        ctx(halt=HaltState(True, HaltScope.ALL, "x"), working_keys={PUT.key}),
    )
    assert failed(d) == {"halt", "price_in_market", "no_duplicate"}
    assert d.summary().startswith("rejected: ")
    assert d.to_dict()["approved"] is False
