"""Recovery automation: covered calls need no margin, no forced buy-backs.

* A fully covered call skips the margin-after checks (it can't lower Excess
  Liquidity), so the bot keeps selling calls while the account is low.
* ``entry.cc_all_holdings`` sells calls on held stocks outside the watchlist.
* With ``margin_floor.auto`` off, a breach sends advice instead of buying
  options back (except below the emergency level).
"""
import asyncio

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.config import MarginFloorConfig, TradingConfig
from src.trading.domain import Greeks, Position, Quote, StockContract, set_clock
from src.trading.executor import Executor
from src.trading.journal import Journal
from src.trading.risk.limits import exposures_from, fully_covered_calls, gate_decision
from src.trading.scanner import uncovered_lots
from tests.trading.test_limits import (
    CONFIG,
    HOLDINGS,
    QUOTES,
    SPY,
    SPY_CALL,
    ctx,
    intent,
    xsp_put,
)
from tests.trading.test_executor import MID_SESSION, PUT


def test_fully_covered_calls_counts_existing_short_calls():
    ex = exposures_from(HOLDINGS, QUOTES)  # 400 SPY
    assert fully_covered_calls(intent(SPY_CALL, qty=4), ex)
    assert not fully_covered_calls(intent(SPY_CALL, qty=5), ex)
    with_calls = exposures_from(HOLDINGS + [Position(SPY_CALL, -3, 1.0)], QUOTES)
    assert fully_covered_calls(intent(SPY_CALL, qty=1), with_calls)
    assert not fully_covered_calls(intent(SPY_CALL, qty=2), with_calls)
    assert not fully_covered_calls(intent(xsp_put()), ex)


def test_covered_call_passes_with_low_excess_liquidity():
    low = ctx(el_after=20_000)
    names = {
        c.name for c in gate_decision(intent(SPY_CALL, qty=1), low, CONFIG).failures
    }
    assert "el_after" not in names and "cushion_after" not in names
    put_fails = {c.name for c in gate_decision(intent(xsp_put()), low, CONFIG).failures}
    assert {"el_after", "cushion_after"} <= put_fails
    # Uncovered (more calls than shares): the margin checks apply again.
    big = {c.name for c in gate_decision(intent(SPY_CALL, qty=5), low, CONFIG).failures}
    assert "el_after" in big


def test_cc_all_holdings():
    held = [Position(StockContract("ABC"), 400, 50.0), Position(SPY, 200, 500.0)]
    assert uncovered_lots(held, TradingConfig()) == {}
    cfg = TradingConfig.model_validate({"entry": {"cc_all_holdings": True}})
    assert uncovered_lots(held, cfg) == {"ABC": 2, "SPY": 1}  # 50% coverage


class Rec:
    def __init__(self):
        self.sent = []

    def notify(self, level, key, title, message):
        self.sent.append((level, key, title, message))

    async def anotify(self, *a):
        self.notify(*a)


def _breach(tmp_path, monkeypatch, auto):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    monkeypatch.setattr(Executor, "in_window", lambda self, intent: True)
    broker = FakeAdapter(account_id="DU1", read_only=False)
    broker.set_account(excess_liquidity=95_000, net_liquidation=200_000)
    broker.set_position(PUT, -2, 2.0)
    broker.set_quote(
        Quote(PUT, 1.00, 1.02, MID_SESSION, underlying_price=650, greeks=Greeks(-0.1))
    )

    async def no_sleep(_):
        return None

    real_om = run.OrderManager
    monkeypatch.setattr(
        run, "OrderManager", lambda *a, **kw: real_om(*a, sleep=no_sleep, **kw)
    )
    config = TradingConfig(margin_floor=MarginFloorConfig(auto=auto))
    notes = Rec()
    set_clock(lambda: MID_SESSION)
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
            config=config,
            notifier=notes,
        )
    )
    set_clock(None)
    asyncio.run(broker.connect())
    return asyncio.run(broker.positions()), notes


def test_breach_without_auto_alerts_and_keeps_the_option(tmp_path, monkeypatch):
    positions, notes = _breach(tmp_path, monkeypatch, auto=False)
    assert [p.contract.key for p in positions] == [PUT.key]  # nothing bought back
    advice = [s for s in notes.sent if s[1] == "restoration-advice"]
    assert advice and f"buy 2 {PUT.key}" in advice[0][3]


def test_breach_with_auto_buys_back(tmp_path, monkeypatch):
    positions, _ = _breach(tmp_path, monkeypatch, auto=True)
    assert positions == []


def test_stress_gate_lets_covered_calls_through_a_failing_account():
    """The account fails the crash test (naked puts): calls still pass, puts don't."""
    from datetime import date

    from src.trading.domain import AccountSnapshot
    from src.trading.risk.stress import stress_checks
    from tests.trading.test_limits import NOW

    acct = AccountSnapshot("DU1", NOW, 400_000, 48_000, 300_000, 352_000, 40_000)
    ex = exposures_from(HOLDINGS + [Position(xsp_put(), -30, 3.0)], QUOTES)
    today = date(2026, 11, 23)
    call = stress_checks(intent(SPY_CALL, qty=1), ex, QUOTES, acct, CONFIG, today)
    assert call.approved, call.summary()
    put = stress_checks(intent(xsp_put()), ex, QUOTES, acct, CONFIG, today)
    assert {c.name for c in put.failures} >= {"stress_budget", "stress_el"}
    too_many = stress_checks(intent(SPY_CALL, qty=5), ex, QUOTES, acct, CONFIG, today)
    assert not too_many.approved  # 500 calls on 400 shares: not covered
