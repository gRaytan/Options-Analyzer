"""Opportunity scanner (plan T30, spec §5.9, R3).

Three queues: **covered calls** (holdings with uncovered shares, under the
per-symbol coverage cap), **index puts** (XSP by default) and **single-stock
puts** (watchlist names enabled for ``stock_put``).

For each underlying: expiries in the entry window, strikes in the delta
band, then hard filters, then a score:

* liquidity: spread <= 10% of mid (and <= $0.10 when mid < $1); open
  interest when known (per-symbol override, e.g. QQQM 50);
* volatility edge: IV / max(RV20, RV60) >= 1.10, and IV rank >= 25 (index) /
  30 (stocks);
* net credit after round-trip fees >= $20 and >= 5x the fees;
* **expected value under realized volatility** > 0: the premium minus the
  expected payout if the stock moves like it has been moving (Black-Scholes
  at max(RV20, RV60) x 1.1), so only options priced *above* realized moves pass;
* covered calls: strike >= the shares' cost basis; skipped in a strong
  uptrend unless IV rank >= 60.

Score = EV / crash-scenario loss x 365 / DTE (puts), or EV / share value x
365 / DTE (covered calls, whose crash loss is the stock's own), times a
liquidity factor and the timing multiplier from the tracker (T31, 1.0 here).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

from py_vollib.black_scholes import black_scholes

from src.trading.config import StrategyName, TradingConfig
from src.trading.domain import (
    AccountSnapshot,
    Action,
    ExerciseStyle,
    OptionContract,
    OrderLeg,
    OrderRequest,
    Position,
    Quote,
    Right,
    StockContract,
)
from src.trading.fees import estimate_order_fees
from src.trading.indicators import Indicators
from src.trading.risk.limits import exposures_from
from src.trading.risk.stress import RATE, Scenario, StressModel

log = logging.getLogger("trading.scanner")

INDEX_ROOTS = {"XSP", "SPX", "SPXW"}
RV_SAFETY = 1.10


@dataclass(frozen=True)
class Candidate:
    strategy: StrategyName
    contract: OptionContract
    quote: Quote
    contracts: int
    credit: float  # $ per contract at mid
    net_credit: float  # $ per contract after round-trip fees
    ev: float  # $ per contract under realized vol
    stress_loss: float  # $ per contract in the budget scenario (puts)
    annual_yield: float  # premium / capital x 365 / DTE
    score: float
    dte: int
    raw_score: float = 0.0  # before the timing multiplier
    timing: float = 1.0
    fire: bool = False  # ready to sell now (tracker, T31)
    timing_reasons: tuple = ()
    # Put credit spreads (T34): the protective long put (same expiry, lower strike).
    long_contract: Optional[OptionContract] = None
    long_quote: Optional[Quote] = None

    @property
    def is_spread(self) -> bool:
        return self.long_contract is not None

    @property
    def width(self) -> float:
        """$ per share between the strikes (0 for a single option)."""
        if self.long_contract is None:
            return 0.0
        return self.contract.strike - self.long_contract.strike

    @property
    def max_loss(self) -> Optional[float]:
        """$ per contract a spread can lose at most (None: unlimited/naked)."""
        if self.long_contract is None:
            return None
        return self.width * self.contract.multiplier - self.credit

    def to_dict(self) -> dict:
        q = self.quote
        return {
            "strategy": self.strategy.value,
            "key": self.contract.key,
            "contracts": self.contracts,
            "bid": q.bid,
            "ask": q.ask,
            "delta": q.greeks.delta if q.greeks else None,
            "iv": q.greeks.iv if q.greeks else None,
            "credit": self.credit,
            "net_credit": self.net_credit,
            "ev": self.ev,
            "stress_loss": self.stress_loss,
            "annual_yield": self.annual_yield,
            "score": self.score,
            "raw_score": self.raw_score or self.score,
            "timing": self.timing,
            "fire": self.fire,
            "timing_reasons": list(self.timing_reasons),
            "dte": self.dte,
            "long_key": self.long_contract.key if self.long_contract else None,
            "width": self.width,
        }


@dataclass
class ScanResult:
    candidates: List[Candidate] = field(default_factory=list)
    rejected: Dict[str, int] = field(default_factory=dict)  # reason -> count

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def best(self, n: int = 10) -> List[Candidate]:
        return sorted(self.candidates, key=lambda c: -c.score)[:n]

    def ready(self) -> List[Candidate]:
        """Candidates the tracker says to sell now, best first."""
        return [c for c in self.best(len(self.candidates)) if c.fire]


# -- helpers --------------------------------------------------------------------


REPORT_CORE = (
    StrategyName.COVERED_CALL,
    StrategyName.INDEX_PUT,
    StrategyName.PUT_SPREAD,
)


def report_strategies(config: TradingConfig) -> set:
    """What the daily opportunities report scans: the core strategies, every
    strategy enabled to open, and single-stock puts when the watchlist has any."""
    out = set(REPORT_CORE)
    out |= {name for name, switch in config.enabled_strategies.items() if switch.open}
    if any(StrategyName.STOCK_PUT in w.strategies for w in config.watchlist.values()):
        out.add(StrategyName.STOCK_PUT)
    out.discard(StrategyName.TAIL_HEDGE)
    return out


def _watch(config: TradingConfig, symbol: str):
    return config.watchlist.get(symbol.upper())


def delta_band(config: TradingConfig, strategy: StrategyName, symbol: str) -> tuple:
    e = config.entry
    if strategy is StrategyName.COVERED_CALL:
        w = _watch(config, symbol)
        return tuple(w.cc_delta) if w and w.cc_delta else tuple(e.cc_delta)
    if strategy is StrategyName.INDEX_PUT:
        return tuple(e.index_put_delta)
    if strategy is StrategyName.PUT_SPREAD:
        return tuple(e.spread_short_delta)
    return tuple(e.stock_put_delta)


def uncovered_lots(
    positions: Iterable[Position], config: TradingConfig
) -> Dict[str, int]:
    """Covered-call lots (100 shares) still available per symbol, under the cap."""
    shares: Dict[str, float] = {}
    short_calls: Dict[str, float] = {}
    for p in positions:
        c = p.contract
        if isinstance(c, StockContract) and p.quantity > 0:
            shares[c.symbol] = shares.get(c.symbol, 0) + p.quantity
        elif isinstance(c, OptionContract) and c.right is Right.CALL and p.quantity < 0:
            short_calls[c.underlying] = short_calls.get(c.underlying, 0) + abs(
                p.quantity
            )
    out = {}
    for sym, n in shares.items():
        w = _watch(config, sym)
        if w is None or StrategyName.COVERED_CALL not in w.strategies:
            continue
        cap = (
            w.cc_max_coverage
            if w.cc_max_coverage is not None
            else config.entry.cc_max_coverage
        )
        lots = int(n * cap // 100) - int(short_calls.get(sym, 0))
        if lots > 0:
            out[sym] = lots
    return out


def _liquid(q: Quote, config: TradingConfig, symbol: str, index: bool) -> Optional[str]:
    e = config.entry
    w = _watch(config, symbol)
    max_pct = w.max_spread_pct if w and w.max_spread_pct else e.max_spread_pct
    if q.mid is None or q.mid <= 0 or q.delayed:
        return "no live market"
    if q.spread_pct is not None and q.spread_pct > max_pct:
        return "spread too wide"
    if q.mid < 1.0 and q.spread is not None and q.spread > e.max_spread_cheap:
        return "spread too wide"
    min_oi = (
        w.min_oi
        if w and w.min_oi is not None
        else (100 if index else e.min_open_interest)
    )
    if q.open_interest is not None and q.open_interest < min_oi:
        return "open interest too low"
    return None


def expected_payout(
    contract: OptionContract, und: float, sigma: float, dte: int
) -> float:
    """Expected value at expiry if the stock moves like its realized volatility."""
    t = max(dte, 1) / 365.0
    flag = "c" if contract.right is Right.CALL else "p"
    return float(black_scholes(flag, und, contract.strike, t, RATE, max(sigma, 0.01)))


def evaluate(
    strategy: StrategyName,
    contract: OptionContract,
    quote: Quote,
    ind: Optional[Indicators],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    contracts: int = 1,
    cost_basis: Optional[float] = None,
    timing_mult: float = 1.0,
) -> tuple:
    """(Candidate, None) if it passes every filter, else (None, reason)."""
    e = config.entry
    sym = contract.underlying
    index = sym.upper() in INDEX_ROOTS
    dte = contract.days_to_expiry(today)
    lo_dte = e.cc_dte_min if strategy is StrategyName.COVERED_CALL else e.dte_min
    if not lo_dte <= dte <= e.dte_max:
        return None, "outside the DTE window"
    if quote.greeks is None or quote.underlying_price is None:
        return None, "no Greeks"
    lo, hi = delta_band(config, strategy, sym)
    if not lo <= abs(quote.greeks.delta) <= hi:
        return None, "delta outside the band"
    reason = _liquid(quote, config, sym, index)
    if reason:
        return None, reason
    if ind is None or ind.iv is None:
        return None, "no volatility history"
    edge = ind.vol_edge
    if edge is None or edge < e.min_vol_edge:
        return None, "options not rich vs realized moves"
    ivr_min = e.ivr_min_index if index else e.ivr_min_stock
    if ind.iv_rank is None or ind.iv_rank < ivr_min:
        return None, "IV rank too low"
    if strategy is StrategyName.COVERED_CALL:
        if (
            config.entry.cc_strike_above_cost
            and cost_basis
            and contract.strike < cost_basis
        ):
            return None, "strike below cost basis"
    mult = contract.multiplier
    credit = quote.mid * mult
    order = OrderRequest(
        "probe", (OrderLeg(contract, Action.SELL, 1),), -quote.mid, "probe"
    )
    fees_one_way = estimate_order_fees(
        order, {contract.key: quote.mid}, config.fees
    ).total
    round_trip = 2 * fees_one_way  # assume it may be bought back once
    net = credit - round_trip
    if net < e.min_net_credit_per_contract:
        return None, "credit too small"
    if net < e.min_fee_multiple * round_trip:
        return None, "fees too large vs credit"
    sigma = max(v for v in (ind.rv20, ind.rv60, 0.0) if v is not None) * RV_SAFETY
    ev = net - expected_payout(contract, quote.underlying_price, sigma, dte) * mult
    if ev <= 0:
        return None, "no positive expected value"
    liquidity = 1.0 - min(quote.spread_pct or 0.0, 0.5)
    if strategy is StrategyName.COVERED_CALL:
        capital = quote.underlying_price * mult
        stress = 0.0
        score = ev / capital * 365 / dte * liquidity * timing_mult
        annual_yield = credit / capital * 365 / dte
    else:
        pos = Position(contract, -1, quote.mid, quote.mid)
        model = StressModel(
            exposures_from([pos], {contract.key: quote}),
            {contract.key: quote},
            account,
            today,
        )
        stress = max(
            0.0,
            -model.run(
                Scenario("budget", -0.20, 0.50, config.risk.stress_margin_multiplier)
            ).options_pnl,
        )
        if stress <= 0:
            return None, "no crash loss estimate"
        score = ev / stress * 365 / dte * liquidity * timing_mult
        capital = (
            0.15 * quote.underlying_price * mult
        )  # Reg-T-ish margin, for the yield display
        annual_yield = credit / capital * 365 / dte
    return (
        Candidate(
            strategy,
            contract,
            quote,
            contracts,
            credit,
            net,
            ev,
            stress,
            annual_yield,
            score,
            dte,
        ),
        None,
    )


def thin(strikes: Sequence[float], n: int) -> List[float]:
    """At most ``n`` strikes, keeping order: round ($5) strikes first, then evenly spaced."""
    strikes = list(strikes)
    if len(strikes) <= n:
        return strikes
    round5 = [k for k in strikes if abs(k / 5 - round(k / 5)) < 1e-9]
    if len(round5) >= n // 2:
        strikes = round5
    if len(strikes) <= n:
        return strikes
    step = len(strikes) / n
    return [strikes[int(i * step)] for i in range(n)]


def _spread_filters(
    short_q: Quote, ind: Optional[Indicators], config: TradingConfig, today: date, band
) -> tuple:
    """(dte, None) when the short leg passes the shared entry filters, else (None, reason)."""
    e = config.entry
    c = short_q.contract
    dte = c.days_to_expiry(today)
    if not e.dte_min <= dte <= e.dte_max:
        return None, "outside the DTE window"
    if short_q.greeks is None or short_q.underlying_price is None:
        return None, "no Greeks"
    if not band[0] <= abs(short_q.greeks.delta) <= band[1]:
        return None, "delta outside the band"
    reason = _liquid(short_q, config, c.underlying, True)
    if reason:
        return None, reason
    if ind is None or ind.iv is None:
        return None, "no volatility history"
    if ind.vol_edge is None or ind.vol_edge < e.min_vol_edge:
        return None, "options not rich vs realized moves"
    if ind.iv_rank is None or ind.iv_rank < e.ivr_min_index:
        return None, "IV rank too low"
    return dte, None


def evaluate_spread(
    short_q: Quote,
    long_q: Quote,
    ind: Optional[Indicators],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    band: Optional[tuple] = None,
) -> tuple:
    """A put credit spread (spec §5.4): (Candidate, None) or (None, reason).

    Short leg in the spread delta band; long leg on the same expiry at a
    lower strike; credit >= ``spread_min_credit_width`` x width; the same
    credit, fee and expected-value tests as single puts. The crash loss is
    capped by the width, so the score is EV per $ of (much smaller) risk.
    """
    e = config.entry
    sc, lc = short_q.contract, long_q.contract
    band = band or tuple(e.spread_short_delta)
    dte, reason = _spread_filters(short_q, ind, config, today, band)
    if reason:
        return None, reason
    if lc.expiry != sc.expiry or lc.strike >= sc.strike or lc.right is not Right.PUT:
        return None, "not a put spread"
    width = sc.strike - lc.strike
    if width > e.spread_max_width:
        return None, "spread too wide"
    if long_q.mid is None or long_q.mid < 0 or long_q.delayed:
        return None, "no live market"
    if long_q.spread is not None and long_q.spread > max(
        e.max_spread_cheap, e.max_spread_pct * long_q.mid
    ):
        return None, "spread too wide"
    mult = sc.multiplier
    net_mid = short_q.mid - long_q.mid
    credit = net_mid * mult
    if credit < e.spread_min_credit_width * width * mult:
        return None, "credit too small vs the width"
    order = OrderRequest(
        "probe",
        (OrderLeg(sc, Action.SELL, 1), OrderLeg(lc, Action.BUY, 1)),
        -net_mid,
        "probe",
    )
    fees = estimate_order_fees(
        order, {sc.key: short_q.mid, lc.key: long_q.mid}, config.fees
    ).total
    round_trip = 2 * fees
    net = credit - round_trip
    if net < e.min_net_credit_per_contract:
        return None, "credit too small"
    if net < e.min_fee_multiple * round_trip:
        return None, "fees too large vs credit"
    sigma = max(v for v in (ind.rv20, ind.rv60, 0.0) if v is not None) * RV_SAFETY
    und = short_q.underlying_price
    payout = expected_payout(sc, und, sigma, dte) - expected_payout(lc, und, sigma, dte)
    ev = net - payout * mult
    if ev <= 0:
        return None, "no positive expected value"
    max_loss = width * mult - credit
    quotes = {sc.key: short_q, lc.key: long_q}
    legs = [
        Position(sc, -1, short_q.mid, short_q.mid),
        Position(lc, 1, long_q.mid, long_q.mid),
    ]
    model = StressModel(exposures_from(legs, quotes), quotes, account, today)
    stress = -model.run(
        Scenario("budget", -0.20, 0.50, config.risk.stress_margin_multiplier)
    ).options_pnl
    stress = min(max(stress, 0.0), max_loss)
    if stress <= 0:
        stress = max_loss  # can't be less than nothing at risk
    if stress <= 0:
        return None, "no crash loss estimate"
    liquidity = 1.0 - min(short_q.spread_pct or 0.0, 0.5)
    score = ev / stress * 365 / dte * liquidity
    return (
        Candidate(
            StrategyName.PUT_SPREAD,
            sc,
            short_q,
            1,
            credit,
            net,
            ev,
            stress,
            credit / max_loss * 365 / dte if max_loss > 0 else 0.0,
            score,
            dte,
            long_contract=lc,
            long_quote=long_q,
        ),
        None,
    )


def best_spreads(
    quotes: Sequence[Quote],
    ind: Optional[Indicators],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    regime: str,
    result: "ScanResult",
) -> List[Candidate]:
    """For each short strike, the best-scoring long leg below it."""
    band = (
        tuple(config.entry.spread_fading_delta)
        if regime == "fading"
        else tuple(config.entry.spread_short_delta)
    )
    by_expiry: Dict[date, List[Quote]] = {}
    for q in quotes:
        by_expiry.setdefault(q.contract.expiry, []).append(q)
    out = []
    for group in by_expiry.values():
        for sq in group:
            if sq.greeks is None or not band[0] <= abs(sq.greeks.delta) <= band[1]:
                result.reject("delta outside the band")
                continue
            best, reason = None, "no long leg"
            for lq in group:
                if lq.contract.strike >= sq.contract.strike:
                    continue
                cand, why = evaluate_spread(sq, lq, ind, account, config, today, band)
                if cand is None:
                    reason = why
                elif best is None or cand.score > best.score:
                    best = cand
            if best is None:
                result.reject(reason)
            else:
                out.append(best)
    return out


def candidate_contracts(
    underlying: str,
    right: Right,
    chains: Sequence,
    underlying_price: float,
    today: date,
    dte_min: int,
    dte_max: int,
    trading_class: Optional[str] = None,
    max_strikes: int = 12,
    sigma: Optional[float] = None,
    extra_below: float = 0.0,
) -> List[OptionContract]:
    """OTM strikes (calls above, puts below the price) in the DTE window.

    With ``sigma`` (implied vol), strikes come from the moneyness window where
    the entry deltas live (about 0.5 to 2.5 standard moves out, plus
    ``extra_below`` $ for spread long legs), thinned to ``max_strikes``
    (round strikes first). Without it: the ``max_strikes`` nearest OTM strikes.
    """
    chain = None
    for c in chains:
        if trading_class is None or c.trading_class == trading_class:
            chain = c
            break
    if chain is None:
        return []
    index = underlying.upper() in INDEX_ROOTS
    expiries = [e for e in chain.expiries if dte_min <= (e - today).days <= dte_max][:4]
    if right is Right.PUT:
        strikes = sorted(
            (k for k in chain.strikes if k < underlying_price), reverse=True
        )
    else:
        strikes = sorted(k for k in chain.strikes if k > underlying_price)
    if sigma and sigma > 0:
        near = sigma * math.sqrt(dte_min / 365.0) * 0.5
        far = sigma * math.sqrt(dte_max / 365.0) * 2.5
        if right is Right.PUT:
            lo = underlying_price * (1 - far) - extra_below
            hi = underlying_price * (1 - near)
        else:
            lo = underlying_price * (1 + near)
            hi = underlying_price * (1 + far)
        strikes = thin([k for k in strikes if lo <= k <= hi], max_strikes)
    else:
        strikes = strikes[:max_strikes]
    return [
        OptionContract(
            underlying,
            e,
            k,
            right,
            chain.multiplier,
            ExerciseStyle.EUROPEAN if index else ExerciseStyle.AMERICAN,
            index,
            chain.trading_class,
        )
        for e in expiries
        for k in strikes
    ]


async def _underlying_view(history, symbol: str):
    """(last price, Indicators) from daily price and IV history."""
    from src.trading.domain import (
        HistoryKind,
    )  # pylint: disable=import-outside-toplevel
    from src.trading import indicators  # pylint: disable=import-outside-toplevel

    bars = await history.daily(symbol, HistoryKind.PRICE, 365)
    ivs = [b.close for b in await history.daily(symbol, HistoryKind.IMPLIED_VOL, 365)]
    if not bars:
        return None, None
    return bars[-1].close, indicators.compute(bars, ivs)


async def scan(
    broker,
    history,
    positions: Sequence[Position],
    account: AccountSnapshot,
    config: TradingConfig,
    today: date,
    events=None,
    journal=None,
    tracker=None,
    regime: str = "unknown",
    strategies=None,
) -> ScanResult:
    """Run all three queues and return the passing candidates (best first via .best()).

    With a ``tracker`` (T31), each underlying's candidates get its timing
    multiplier and a ``fire`` flag; without one, nothing is marked ready.
    ``strategies``: which queues to scan (default: those enabled to open);
    the daily opportunities report scans more, for display only.
    """
    result = ScanResult()
    work = []  # (strategy, symbol, right, contracts, cost_basis, trading_class)
    sw = config.enabled_strategies
    wanted = (
        set(strategies)
        if strategies is not None
        else {name for name, switch in sw.items() if switch.open}
    )
    if StrategyName.COVERED_CALL in wanted:
        cost = {
            p.contract.symbol: p.avg_cost
            for p in positions
            if isinstance(p.contract, StockContract)
        }
        for sym, lots in uncovered_lots(positions, config).items():
            work.append(
                (StrategyName.COVERED_CALL, sym, Right.CALL, lots, cost.get(sym), sym)
            )
    if StrategyName.INDEX_PUT in wanted:
        root = config.entry.index_scan_root
        work.append((StrategyName.INDEX_PUT, root, Right.PUT, 1, None, root))
    if StrategyName.PUT_SPREAD in wanted:
        root = config.entry.index_scan_root
        work.append((StrategyName.PUT_SPREAD, root, Right.PUT, 1, None, root))
    if StrategyName.STOCK_PUT in wanted:
        for sym, w in config.watchlist.items():
            if StrategyName.STOCK_PUT in w.strategies:
                work.append((StrategyName.STOCK_PUT, sym, Right.PUT, 1, None, sym))

    for strategy, sym, right, lots, cost_basis, tclass in work:
        try:
            price, ind = await _underlying_view(history, sym)
            if price is None:
                result.reject("no price history")
                continue
            chains = await broker.option_chain(sym)
            lo = (
                config.entry.cc_dte_min
                if strategy is StrategyName.COVERED_CALL
                else config.entry.dte_min
            )
            spread = strategy is StrategyName.PUT_SPREAD
            contracts = candidate_contracts(
                sym,
                right,
                chains,
                price,
                today,
                lo,
                config.entry.dte_max,
                tclass,
                max_strikes=20 if ind is not None and ind.iv else 12,
                sigma=ind.iv if ind is not None else None,
                extra_below=config.entry.spread_max_width if spread else 0.0,
            )
            if not contracts:
                result.reject("no contracts in the window")
                continue
            quotes = await broker.quotes(contracts)
        except Exception as exc:  # pylint: disable=broad-except
            log.warning("scan %s %s failed: %s", strategy.value, sym, exc)
            result.reject("data unavailable")
            continue
        # Uptrend rule for covered calls (spec §5.3).
        if (
            strategy is StrategyName.COVERED_CALL
            and config.entry.cc_skip_strong_uptrend
            and ind is not None
            and ind.uptrend
            and ind.close
            and (ind.iv_rank or 0) < 60
        ):
            from src.trading.domain import (
                HistoryKind,
            )  # pylint: disable=import-outside-toplevel

            closes = [b.close for b in await history.daily(sym, HistoryKind.PRICE, 365)]
            if ind.strong_uptrend(closes):
                result.reject("strong uptrend: covered calls skipped")
                continue
        passed = []
        if strategy is StrategyName.PUT_SPREAD:
            passed = best_spreads(quotes, ind, account, config, today, regime, result)
            quotes = []  # handled above
        for q in quotes:
            c = q.contract
            if events is not None and sym.upper() not in INDEX_ROOTS:
                if events.earnings_before(sym, c.expiry, today) is not False:
                    result.reject("earnings before expiry (or unknown)")
                    continue
            cand, reason = evaluate(
                strategy, c, q, ind, account, config, today, lots, cost_basis
            )
            if cand is None:
                result.reject(reason)
            else:
                passed.append(cand)
        if passed and tracker is not None:
            from src.trading.domain import (
                HistoryKind,
            )  # pylint: disable=import-outside-toplevel

            bars = await history.daily(sym, HistoryKind.PRICE, 365)
            best_raw = max(c.score for c in passed)
            t = tracker.timing(strategy, sym, ind, bars, regime, best_raw, today)
            passed = [
                replace(
                    c,
                    raw_score=c.score,
                    score=c.score * t.multiplier,
                    timing=t.multiplier,
                    fire=t.fire,
                    timing_reasons=t.reasons,
                )
                for c in passed
            ]
        else:
            passed = [replace(c, raw_score=c.score) for c in passed]
        result.candidates.extend(passed)
    if journal is not None:
        journal.append(
            "scan_results",
            {
                "date": today.isoformat(),
                "best": [c.to_dict() for c in result.best(20)],
                "rejected": result.rejected,
                "regime": regime,
                # Strategies scanned for the report but not enabled to open.
                "report_only": sorted(s.value for s in wanted if not sw[s].open),
            },
        )
    return result
