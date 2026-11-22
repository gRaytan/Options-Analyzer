"""Mode gating policy (plan T28, spec §11)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.broker.ib_adapter import (
    IBAdapter,
    IBConnectionSettings,
    LiveTradingNotAllowed,
)
from src.trading.config import Mode, TradingConfig
from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Right,
    TradeIntent,
    utcnow,
)
from src.trading.mode_policy import Route, connection_read_only, live_allowed, route

NOW = utcnow()
SOON = OptionContract("XSP", NOW.date() + timedelta(days=1), 600, Right.PUT)
LATER = OptionContract("XSP", NOW.date() + timedelta(days=40), 590, Right.PUT)
YES = {"TRADING_ALLOW_LIVE": "yes"}


def cfg(mode, **kw):
    return TradingConfig.model_validate({"mode": mode, **kw})


def intent(kind, strategy="index_put", risk_reducing=False, legs=None):
    legs = legs or (OrderLeg(LATER, Action.SELL, 1),)
    return TradeIntent("i", strategy, kind, legs, "T", NOW, risk_reducing)


OPEN = intent(IntentKind.OPEN)
CLOSE = intent(
    IntentKind.CLOSE, risk_reducing=True, legs=(OrderLeg(LATER, Action.BUY, 1),)
)
RESTORE = intent(
    IntentKind.CLOSE, "margin_restoration", True, (OrderLeg(LATER, Action.BUY, 1),)
)
ROLL = intent(
    IntentKind.ROLL,
    legs=(OrderLeg(LATER, Action.BUY, 1), OrderLeg(LATER, Action.SELL, 1)),
)
URGENT_ROLL = intent(
    IntentKind.ROLL,
    legs=(OrderLeg(SOON, Action.BUY, 1), OrderLeg(LATER, Action.SELL, 1)),
)

LIVE_ON = {"live_trading_enabled": True}


@pytest.mark.parametrize(
    "mode,extra,is_live,env,it,expected",
    [
        # paper mode: paper account sends everything; a live account never sends
        ("paper", {}, False, {}, OPEN, Route.SEND),
        ("paper", LIVE_ON, True, YES, OPEN, Route.LOG),
        # shadow: log everything
        ("shadow", LIVE_ON, True, YES, RESTORE, Route.LOG),
        # approve: ask, except restoration and rolls at <= 1 DTE
        ("approve", LIVE_ON, True, YES, OPEN, Route.ASK),
        ("approve", LIVE_ON, True, YES, CLOSE, Route.ASK),
        ("approve", LIVE_ON, True, YES, ROLL, Route.ASK),
        ("approve", LIVE_ON, True, YES, URGENT_ROLL, Route.SEND),
        ("approve", LIVE_ON, True, YES, RESTORE, Route.SEND),
        # auto: manage automatically; entries need approval unless auto_open
        ("auto", LIVE_ON, True, YES, CLOSE, Route.SEND),
        ("auto", LIVE_ON, True, YES, ROLL, Route.SEND),
        ("auto", LIVE_ON, True, YES, OPEN, Route.ASK),
        ("auto", {**LIVE_ON, "auto_open": True}, True, YES, OPEN, Route.SEND),
        # missing either key: a live account only logs
        ("auto", {}, True, YES, RESTORE, Route.LOG),
        ("auto", LIVE_ON, True, {}, RESTORE, Route.LOG),
        ("approve", LIVE_ON, True, {"TRADING_ALLOW_LIVE": "no"}, RESTORE, Route.LOG),
        # paper account in later modes behaves like that mode
        ("approve", {}, False, {}, OPEN, Route.ASK),
    ],
)
def test_route_matrix(mode, extra, is_live, env, it, expected):
    assert route(it, cfg(mode, **extra), is_live, env) is expected


def test_live_needs_both_keys():
    assert not live_allowed(cfg("auto"), YES)
    assert not live_allowed(cfg("auto", **LIVE_ON), {})
    assert live_allowed(cfg("auto", **LIVE_ON), YES)


@pytest.mark.parametrize(
    "mode,extra,is_live,env,read_only",
    [
        ("paper", {}, False, {}, False),
        ("paper", LIVE_ON, True, YES, True),
        ("shadow", {}, False, {}, True),
        ("approve", LIVE_ON, True, YES, False),
        ("approve", {}, True, YES, True),
        ("auto", LIVE_ON, True, {}, True),
    ],
)
def test_connection_read_only(mode, extra, is_live, env, read_only):
    assert connection_read_only(cfg(mode, **extra), is_live, env) is read_only


def test_ib_adapter_live_needs_allow_live():
    from tests.trading.test_ib_adapter import FakeIB

    with pytest.raises(LiveTradingNotAllowed):
        IBAdapter(
            "U123", IBConnectionSettings(port=4001), read_only=False, ib_factory=FakeIB
        )
    a = IBAdapter(
        "U123",
        IBConnectionSettings(port=4001),
        read_only=False,
        ib_factory=FakeIB,
        allow_live=True,
    )
    assert a.is_live and not a.read_only


def test_order_manager_live_needs_allow_live(tmp_path):
    import asyncio

    from src.trading.broker.fake import FakeAdapter
    from src.trading.journal import Journal
    from src.trading.order_manager import ExecStatus, OrderManager

    broker = FakeAdapter(account_id="U1")
    broker.is_live = True
    asyncio.run(broker.connect())

    async def no_sleep(_):
        return None

    om = OrderManager(broker, Journal(tmp_path / "j.db"), sleep=no_sleep)
    res = asyncio.run(om.execute(OPEN, None, None))
    assert res.status is ExecStatus.REFUSED and "both keys" in res.detail


def test_config_defaults_are_safe():
    c = TradingConfig()
    assert c.mode is Mode.PAPER and not c.live_trading_enabled and not c.auto_open
