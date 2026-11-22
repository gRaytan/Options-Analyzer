"""Tests for broker-independent domain types (plan T03)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

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
    from_dict,
    make_intent_id,
    to_dict,
)

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
XSP_PUT = OptionContract(
    underlying="XSP",
    expiry=date(2026, 12, 31),
    strike=550,
    right=Right.PUT,
    style=ExerciseStyle.EUROPEAN,
    cash_settled=True,
)
QQQM_CALL = OptionContract("QQQM", date(2026, 12, 18), 240, Right.CALL)


def roundtrip(obj):
    return from_dict(json.loads(json.dumps(to_dict(obj))))


def test_option_key_and_properties():
    assert XSP_PUT.key == "XSP 2026-12-31 P 550"
    assert not XSP_PUT.can_be_assigned_early
    assert QQQM_CALL.can_be_assigned_early
    assert XSP_PUT.days_to_expiry(date(2026, 11, 21)) == 40
    assert XSP_PUT.intrinsic(540) == 10
    assert XSP_PUT.intrinsic(560) == 0
    assert QQQM_CALL.is_itm(250)
    spxw = OptionContract(
        "SPX", date(2026, 12, 31), 5500, Right.PUT, trading_class="SPXW"
    )
    assert spxw.key.startswith("SPXW ")


def test_quote_mid_spread_and_extrinsic():
    q = Quote(QQQM_CALL, bid=1.0, ask=1.4, ts=T0, underlying_price=241)
    assert q.mid == pytest.approx(1.2)
    assert q.spread_pct == pytest.approx(0.4 / 1.2)
    assert q.extrinsic() == pytest.approx(0.2)
    assert q.age_seconds(T0 + timedelta(seconds=7)) == 7


@pytest.mark.parametrize("bid,ask", [(None, 1.0), (1.0, None), (1.2, 1.0), (-1, 1)])
def test_quote_without_valid_market(bid, ask):
    q = Quote(QQQM_CALL, bid=bid, ask=ask, ts=T0)
    assert q.mid is None
    assert q.spread_pct is None


def test_account_cushion():
    snap = AccountSnapshot("DU1", T0, 1_000_000, 450_000, 300_000, 400_000, 350_000)
    assert snap.cushion == pytest.approx(0.45)
    empty = AccountSnapshot("DU1", T0, 0, 0, 0, 0, 0)
    assert empty.cushion == 0.0


def test_margin_impact_excess_liquidity_after():
    impact = MarginImpact(800_000, 350_000, 300_000)
    assert impact.excess_liquidity_after == 500_000


def test_intent_id_is_deterministic():
    a = make_intent_id("index_put", IntentKind.OPEN, XSP_PUT.key, date(2026, 11, 20))
    b = make_intent_id("index_put", IntentKind.OPEN, XSP_PUT.key, date(2026, 11, 20))
    c = make_intent_id("index_put", IntentKind.OPEN, XSP_PUT.key, date(2026, 11, 21))
    assert a == b != c
    assert len(a) == 16


def test_leg_and_intent_validation():
    with pytest.raises(ValueError):
        OrderLeg(XSP_PUT, Action.SELL, 0)
    with pytest.raises(ValueError):
        TradeIntent("x", "index_put", IntentKind.OPEN, (), "SCAN", T0)
    assert Action.BUY.sign == 1 and Action.SELL.sign == -1


@pytest.mark.parametrize(
    "obj",
    [
        StockContract("QQQM"),
        XSP_PUT,
        Quote(
            XSP_PUT, 1.0, 1.1, T0, underlying_price=600, greeks=Greeks(-0.12, iv=0.18)
        ),
        AccountSnapshot("DU1", T0, 1e6, 4.5e5, 3e5, 4e5, 3.5e5),
        Position(QQQM_CALL, -2, 1.25),
        MarginImpact(8e5, 3.5e5, 3e5, commission=1.3),
        TradeIntent(
            intent_id="abc",
            strategy="covered_call",
            kind=IntentKind.ROLL,
            legs=(
                OrderLeg(QQQM_CALL, Action.BUY, 2),
                OrderLeg(
                    OptionContract("QQQM", date(2027, 1, 15), 250, Right.CALL),
                    Action.SELL,
                    2,
                ),
            ),
            trigger="CALL_DELTA_ROLL",
            created_at=T0,
            risk_reducing=True,
            chain_id="chain-1",
            details={"delta": 0.31, "candidates": [1, 2]},
        ),
        OrderRequest("abc", (OrderLeg(XSP_PUT, Action.SELL, 1),), -1.05, "abc"),
        Fill("e1", "abc", XSP_PUT, Action.SELL, 1, 1.05, T0, commission=0.9),
        HaltState(True, HaltScope.ALL, "manual", T0, "owner"),
    ],
)
def test_json_roundtrip(obj):
    assert roundtrip(obj) == obj
