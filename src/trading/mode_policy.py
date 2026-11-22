"""Mode gating: what the bot may do in each rollout stage (plan T28, spec §11).

| Mode | Account | Openings | Closes, rolls, hedge | Margin restoration |
|---|---|---|---|---|
| ``paper`` | paper only | send | send | send |
| ``shadow`` | live, read-only | log only | log only | log only |
| ``approve`` | live | ask | ask (except rolls at <= 1 DTE: send) | send |
| ``auto`` | live | ask (send with ``auto_open``) | send | send |

* **log** = journaled as a shadow intent, nothing sent; **ask** = the
  approval queue (T27); **send** = the order manager (T22), after the risk gate.
* **Live trading needs two keys**: ``live_trading_enabled: true`` in
  ``trading.json`` **and** ``TRADING_ALLOW_LIVE=yes`` in the environment.
  Without both, any mode other than ``paper``/``shadow`` on a live account
  only logs. ``paper`` mode on a live account never sends.
* Halts (T15) and the risk gate still apply on top of this.
"""

from __future__ import annotations

import os
from datetime import timedelta
from enum import Enum
from typing import Dict, Optional

from src.trading.config import Mode, TradingConfig
from src.trading.domain import Action, IntentKind, TradeIntent, utcnow

ALLOW_LIVE_ENV = "TRADING_ALLOW_LIVE"
RESTORATION_STRATEGY = "margin_restoration"


class Route(str, Enum):
    SEND = "send"
    ASK = "ask"
    LOG = "log"


def live_allowed(config: TradingConfig, env: Optional[Dict[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    return config.live_trading_enabled and env.get(ALLOW_LIVE_ENV, "").lower() == "yes"


def connection_read_only(config: TradingConfig, is_live: bool, env=None) -> bool:
    """Whether the engine must connect read-only in this mode."""
    if config.mode is Mode.SHADOW:
        return True
    if is_live:
        return config.mode is Mode.PAPER or not live_allowed(config, env)
    return False  # paper account: may trade in every mode except shadow


def _is_opening(intent: TradeIntent) -> bool:
    return (
        intent.kind in (IntentKind.OPEN, IntentKind.HEDGE) and not intent.risk_reducing
    )


def _urgent_roll(intent: TradeIntent) -> bool:
    if intent.kind is not IntentKind.ROLL:
        return False
    tomorrow = utcnow().date() + timedelta(days=1)
    closing = [leg.contract for leg in intent.legs if leg.action is Action.BUY]
    return any(getattr(c, "expiry", tomorrow) <= tomorrow for c in closing)


def route(intent: TradeIntent, config: TradingConfig, is_live: bool, env=None) -> Route:
    """Where this intent goes in the current mode."""
    mode = config.mode
    if mode is Mode.SHADOW:
        return Route.LOG
    if is_live and (mode is Mode.PAPER or not live_allowed(config, env)):
        return Route.LOG  # never send to a live account without both keys
    if mode is Mode.PAPER:
        return Route.SEND
    restoration = intent.strategy == RESTORATION_STRATEGY
    if mode is Mode.APPROVE:
        if restoration or _urgent_roll(intent):
            return Route.SEND  # spec §11: automatic even in Stage 3
        return Route.ASK
    # Mode.AUTO
    if _is_opening(intent) and not config.auto_open:
        return Route.ASK
    return Route.SEND
