"""Buy ideas: stocks and options worth buying, sized to what the account can carry.

Report only: nothing here is bought on its own. Stock buys go through the
Rebuild page (each item approved); option buys are listed with their cost.

**Signals** (daily bars, once a day):

| Signal | Rule |
|---|---|
| ``dip in uptrend`` | above the 200-day average, and down >= 2 ATR over 5 days (or RSI(14) <= 30) |
| ``deep drawdown`` | >= 20% below the 52-week high |

**Ideas** for each name with a signal:

* **Shares**: the largest of 5%, 2.5%, 1% of NLV that keeps Excess Liquidity
  above the new-trade floor, Cushion above its minimum and the account
  through the crash test (the rebuild plan's own check). If none fits, the
  idea says which limit binds.
* **Long call instead of shares** (stock replacement), when options are
  cheap (IV rank <= 35): delta about 0.75, 6-13 months out. The most it can
  lose is what it costs, and it can't cause a margin call; it needs the cost
  in Excess Liquidity above the floor.

And one account-wide idea:

* **Cheap crash protection**: when index IV rank <= 20, an XSP put about 10%
  below, 4-8 months out, with how much it would improve Excess Liquidity in
  the crash test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Sequence

from src.trading.config import TradingConfig
from src.trading.domain import AccountSnapshot, Bar, Position, Quote
from src.trading.indicators import Indicators

DIP_ATR = 2.0
DIP_DAYS = 5
RSI_OVERSOLD = 30.0
DRAWDOWN = 0.20
STOCK_SIZES = (0.05, 0.025, 0.01)  # of NLV
CALL_IVR_MAX = 35.0
CALL_DELTA = 0.75
CALL_DTE = (180, 400)
HEDGE_IVR_MAX = 20.0
HEDGE_OTM = 0.10
HEDGE_DTE = (120, 240)


@dataclass
class BuyIdea:
    kind: str  # "shares" | "long_call" | "protection"
    symbol: str
    reasons: List[str]
    fits: bool
    cost: float = 0.0
    shares: int = 0
    key: str = ""  # option contract key
    delta: Optional[float] = None
    note: str = ""
    extra: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def signals(bars: Sequence[Bar], ind: Optional[Indicators]) -> List[str]:
    """Why this name is worth a look as a buy (empty: it isn't)."""
    if ind is None or len(bars) < DIP_DAYS + 1:
        return []
    out = []
    close = bars[-1].close
    above_200 = ind.sma200 is not None and close > ind.sma200
    drop = bars[-1 - DIP_DAYS].close - close
    if above_200 and (
        (ind.atr14 and drop >= DIP_ATR * ind.atr14)
        or (ind.rsi14 is not None and ind.rsi14 <= RSI_OVERSOLD)
    ):
        atrs = drop / ind.atr14 if ind.atr14 else 0.0
        out.append(
            f"dip in uptrend: {drop / bars[-1 - DIP_DAYS].close:-.1%} in {DIP_DAYS} days "
            f"({atrs:.1f} ATR), above the 200-day average"
        )
    high = max(b.close for b in bars[-252:])
    if high > 0 and close <= high * (1 - DRAWDOWN):
        out.append(f"deep drawdown: {close / high - 1:.0%} from the 52-week high")
    return out


def size_shares(
    symbol: str,
    price: float,
    positions: Sequence[Position],
    quotes: Dict[str, Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    reasons: List[str],
) -> BuyIdea:
    """The largest stock buy that fits margin and the crash test."""
    from src.trading import rebuild  # pylint: disable=import-outside-toplevel

    nlv = account.net_liquidation
    binding = ""
    for pct in STOCK_SIZES:
        shares = int(pct * nlv // price)
        if shares <= 0:
            continue
        cost = shares * price
        fit = rebuild.evaluate(
            positions, quotes, account, {symbol: cost}, {symbol: price}, config, today
        )
        binding = rebuild._ok(fit, nlv, config)  # pylint: disable=protected-access
        if not binding:
            return BuyIdea(
                "shares",
                symbol,
                reasons,
                True,
                cost,
                shares,
                note=f"{pct:.1%} of NLV; after it: EL ${fit.el_after:,.0f}, "
                f"crash EL ${fit.stress_el_after:,.0f}. Add it on the Rebuild page.",
            )
    return BuyIdea(
        "shares",
        symbol,
        reasons,
        False,
        note=f"doesn't fit yet: {binding or 'price too high for 1% of NLV'}",
    )


def _floor_room(account: AccountSnapshot, config: TradingConfig) -> float:
    f = config.margin_floor
    return account.excess_liquidity - (f.excess_liquidity_min + f.entry_buffer)


def pick_call(quotes: Sequence[Quote]) -> Optional[Quote]:
    usable = [q for q in quotes if q.greeks is not None and q.ask]
    if not usable:
        return None
    return min(usable, key=lambda q: abs(q.greeks.delta - CALL_DELTA))


def long_call(
    symbol: str,
    ind: Optional[Indicators],
    quotes: Sequence[Quote],
    account: AccountSnapshot,
    config: TradingConfig,
    reasons: List[str],
) -> Optional[BuyIdea]:
    if ind is None or ind.iv_rank is None or ind.iv_rank > CALL_IVR_MAX:
        return None
    q = pick_call(quotes)
    if q is None:
        return None
    cost = q.ask * q.contract.multiplier
    room = _floor_room(account, config)
    fits = cost <= room
    return BuyIdea(
        "long_call",
        symbol,
        reasons + [f"options cheap: IV rank {ind.iv_rank:.0f}"],
        fits,
        cost,
        key=q.contract.key,
        delta=q.greeks.delta,
        note=(
            f"≈ {q.greeks.delta * 100:.0f} shares of exposure for ${cost:,.0f}; "
            "can lose at most its cost, no margin call"
            + ("" if fits else f"; needs ${cost:,.0f} of EL above the floor")
        ),
    )


def protection(
    ivr: Optional[float],
    quote: Optional[Quote],
    crash_el_before: float,
    crash_el_after: float,
) -> Optional[BuyIdea]:
    if ivr is None or ivr > HEDGE_IVR_MAX or quote is None or not quote.ask:
        return None
    cost = quote.ask * quote.contract.multiplier
    gain = crash_el_after - crash_el_before
    return BuyIdea(
        "protection",
        quote.contract.underlying,
        [f"index options cheap: IV rank {ivr:.0f}"],
        gain > cost,
        cost,
        key=quote.contract.key,
        delta=quote.greeks.delta if quote.greeks else None,
        note=f"crash-test Excess Liquidity +${gain:,.0f} per contract for ${cost:,.0f}",
        extra={"crash_gain": gain},
    )
