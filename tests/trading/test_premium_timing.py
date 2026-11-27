"""Premium timing: never sell cheap premium; grade past sell / wait calls."""
from datetime import date, timedelta

from src.trading import premium
from src.trading.config import StrategyName
from src.trading.journal import Journal
from src.trading.tracker import Tracker, percentile_rank
from tests.trading.test_tracker import flat_bars, ind

TODAY = date(2026, 11, 23)
CC = StrategyName.COVERED_CALL


def scan_day(day, y, fire=False, sym="SPY", strategy="covered_call", extra="best"):
    return {
        "date": day.isoformat(),
        extra: [
            {
                "strategy": strategy,
                "key": f"{sym} 2027-01-15 C 720",
                "annual_yield": y,
                "raw_score": 1.0,
                "fire": fire,
                "net_credit": 120.0,
                "delta": 0.12,
            }
        ],
    }


def seed_premiums(journal, values, extra="best"):
    for i, y in enumerate(values):
        day = TODAY - timedelta(days=len(values) - i)
        journal.append("scan_results", scan_day(day, y, extra=extra))


def test_percentile_rank():
    assert percentile_rank([], 1) is None
    assert percentile_rank([1, 2, 3, 4], 3) == 0.75


def test_cheap_premium_waits_even_with_other_signals(tmp_path):
    t = Tracker(Journal(tmp_path / "j.db"))
    seed_premiums(t.journal, [0.10 + 0.01 * i for i in range(20)])  # 10%..29%
    rich_iv = ind(ivr=80)  # IV high: +0.10, would fire on its own
    assert t.timing(CC, "SPY", rich_iv, flat_bars(), "normal", 1.0, TODAY).fire
    cheap = t.timing(CC, "SPY", rich_iv, flat_bars(), "normal", 1.0, TODAY, 0.11)
    assert not cheap.fire and "premium cheap" in cheap.wait
    assert cheap.premium_pct == 0.1


def test_rich_premium_adds_to_timing(tmp_path):
    t = Tracker(Journal(tmp_path / "j.db"))
    seed_premiums(t.journal, [0.10 + 0.01 * i for i in range(20)], "by_underlying")
    top = t.timing(CC, "SPY", ind(), flat_bars(), "normal", 1.0, TODAY, 0.40)
    assert top.fire and any("top" in r for r in top.reasons)
    # Not enough history (< 10 days): the premium rule stays out.
    t2 = Tracker(Journal(tmp_path / "j2.db"))
    seed_premiums(t2.journal, [0.2] * 5)
    t2_t = t2.timing(CC, "SPY", ind(), flat_bars(), "normal", 1.0, TODAY, 0.01)
    assert t2_t.wait == "" and t2_t.premium_pct is None


def days(n):
    return [TODAY - timedelta(days=n - i) for i in range(n)]


def test_series_keeps_best_premium_per_day_and_any_fire():
    d = TODAY
    recs = [scan_day(d, 0.20), scan_day(d, 0.25), scan_day(d, 0.22, fire=True)]
    [(pair, pts)] = premium.daily_series(recs).items()
    assert pair == ("covered_call", "SPY")
    assert len(pts) == 1 and pts[0].premium == 0.25 and pts[0].fire


def test_outcomes_grade_sell_and_wait():
    ds = days(12)
    ys = [0.30, 0.20, 0.20, 0.20, 0.20, 0.20, 0.50, 0.20, 0.20, 0.20, 0.20, 0.20]
    fires = [True] + [False] * 11
    recs = [scan_day(d, y, f) for d, y, f in zip(ds, ys, fires)]
    graded = {o.day: o for o in premium.outcomes(premium.daily_series(recs))}
    first, second = graded[ds[0].isoformat()], graded[ds[1].isoformat()]
    assert first.decision == "sell" and first.grade == "right"  # 0.30 vs <= 0.20 later
    assert second.decision == "wait" and second.grade == "right"  # 0.50 came later
    late_wait = graded[ds[6].isoformat()]  # waited at 0.50, then only 0.20s
    assert late_wait.grade == "wrong"
    assert ds[7].isoformat() not in graded  # fewer than 5 later days
    s = premium.summary(list(graded.values()))
    assert s["sell"] == {"count": 1, "right": 1, "wrong": 0, "hit_rate": 1.0}


def test_snapshot_and_page(tmp_path, monkeypatch):
    from src.web.app import create_app

    j = Journal(tmp_path / "j.db")
    today = date.today()
    for i, d in enumerate(reversed([today - timedelta(days=k) for k in range(1, 21)])):
        j.append("scan_results", scan_day(d, 0.10 + 0.01 * i))
    j.append("scan_results", scan_day(today, 0.40, fire=True))
    snap = premium.report([r.data for r in j.read("scan_results")], today.isoformat())
    [s] = snap["snapshot"]
    assert s["verdict"] == "sell" and s["percentile"] == 1.0 and s["days"] == 20
    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    html = create_app().test_client().get("/bot/premium").get_data(as_text=True)
    assert "Premium timing" in html and "SELL" in html and "100th pct" in html
    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "none.db"))
    html = create_app().test_client().get("/bot/premium").get_data(as_text=True)
    assert "No scans recorded yet" in html
