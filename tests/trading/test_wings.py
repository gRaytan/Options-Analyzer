"""Wings: protective puts under naked short puts free margin without buy-backs."""
from datetime import datetime, timezone

import pytest

from src.trading import wings
from src.trading.domain import (
    Action,
    ExerciseStyle,
    Greeks,
    IntentKind,
    OptionContract,
    OrderLeg,
    Position,
    Quote,
    Right,
    TradeIntent,
)
from src.trading.risk.sanity import OrderContext, check_reduces_position

NOW = datetime(2026, 11, 23, 16, 0, tzinfo=timezone.utc)
EXP = datetime(2026, 12, 31).date()


def xsp(k):
    return OptionContract(
        "XSP", EXP, float(k), Right.PUT, style=ExerciseStyle.EUROPEAN, cash_settled=True
    )


def q(c, bid, ask, und=660.0, delta=-0.15):
    return Quote(c, bid, ask, NOW, underlying_price=und, greeks=Greeks(delta))


SHORT = Position(xsp(620), -10, 3.0, 3.0)
QUOTES = {xsp(620).key: q(xsp(620), 2.9, 3.1)}
for k, ask in ((615, 2.3), (610, 1.8), (600, 1.1), (580, 0.5)):
    QUOTES[xsp(k).key] = q(xsp(k), ask - 0.1, ask, delta=-0.1)
CANDS = [xsp(k) for k in (580, 600, 610, 615)]


def test_best_wing_frees_the_most_net_margin():
    # Naked: max(15% x 660 - 40, 62) + 3 = 65/share -> $65k for 10. A $10 wide
    # spread needs $10k: frees $55k for $1.8k.
    cands = [
        c
        for c in CANDS
        if c.strike in wings.wing_strikes(xsp(620), 660.0, [580, 600, 610, 615])
    ]
    w = wings.best_wing(SHORT, 10, QUOTES, cands)
    assert w.long.strike == 610 and round(w.freed) == 55_000
    assert w.cost == pytest.approx(1800)
    assert w.net > 0 and w.efficiency > 20


def test_naked_puts_skips_shorts_with_a_wing():
    winged = [SHORT, Position(xsp(610), 10, 1.8, 1.8)]
    assert wings.naked_puts(winged, QUOTES) == []
    assert [(p.contract.key, n) for p, n in wings.naked_puts([SHORT], QUOTES)] == [
        (xsp(620).key, 10)
    ]


def test_choose_respects_budget_and_need():
    w = wings.best_wing(SHORT, 10, QUOTES, CANDS)
    assert wings.choose([w], need=10_000, budget=5_000) == [w]
    assert wings.choose([w], need=10_000, budget=1_000) == []  # too expensive today
    assert wings.choose([w], need=0, budget=5_000) == []


def test_wing_strikes_window():
    ks = wings.wing_strikes(xsp(620), 660.0, [500, 560, 580, 600, 615, 620, 625])
    # Within 15% below the price, and at least 1% of it under the short.
    assert ks == [580, 600]
    assert wings.wing_strikes(xsp(620), 660.0, [610, 613, 614]) == [610, 613]


def ctx(held):
    return OrderContext(now=NOW, quotes={}, positions=held)


def wing_intent(long_k=615, qty=10, short_key=None, action=Action.BUY):
    return TradeIntent(
        "w",
        wings.STRATEGY,
        IntentKind.HEDGE,
        (OrderLeg(xsp(long_k), action, qty),),
        "MARGIN_WING",
        NOW,
        risk_reducing=True,
        details={"wing_for": short_key or xsp(620).key},
    )


def test_gate_verifies_a_wing():
    held = {xsp(620).key: -10.0}
    assert check_reduces_position(wing_intent(), ctx(held)).passed
    bad = [
        wing_intent(qty=11),  # more than held short
        wing_intent(long_k=625),  # above the short: not a wing
        wing_intent(action=Action.SELL),  # selling puts is new risk
        wing_intent(short_key="XSP 2027-01-29 P 620"),  # nothing held there
    ]
    for i in bad:
        assert not check_reduces_position(i, ctx(held)).passed, i
    assert not check_reduces_position(wing_intent(), ctx({})).passed
