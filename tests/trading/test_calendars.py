"""Market calendar and corporate events (plan T14)."""
import json
from datetime import date, datetime, timezone

import pytest

from src.trading.calendars import (
    Dividend,
    EventCalendar,
    MarketCalendar,
    SymbolEvents,
)


@pytest.fixture(scope="module")
def cal():
    return MarketCalendar()


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_holidays_and_weekends(cal):
    assert not cal.is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not cal.is_trading_day(date(2026, 12, 25))  # Christmas
    assert not cal.is_trading_day(date(2026, 11, 28))  # Saturday
    assert cal.is_trading_day(date(2026, 11, 27))


def test_early_close_day_after_thanksgiving(cal):
    assert cal.is_early_close(date(2026, 11, 27))
    open_, close = cal.session(date(2026, 11, 27))
    assert close == utc(2026, 11, 27, 18, 0)  # 13:00 ET
    assert not cal.is_early_close(date(2026, 11, 30))
    assert cal.session(date(2026, 11, 26)) is None


def test_dst_session_times(cal):
    # 09:30 ET is 14:30 UTC in winter (EST) and 13:30 UTC in summer (EDT).
    assert cal.session(date(2026, 1, 15))[0] == utc(2026, 1, 15, 14, 30)
    assert cal.session(date(2026, 7, 15))[0] == utc(2026, 7, 15, 13, 30)
    # US DST started 2026-03-08: Monday 2026-03-09 closes at 20:00 UTC.
    assert cal.session(date(2026, 3, 9))[1] == utc(2026, 3, 9, 20, 0)


def test_is_open(cal):
    assert cal.is_open(utc(2026, 11, 23, 15, 0))  # Monday 10:00 ET
    assert not cal.is_open(utc(2026, 11, 23, 22, 0))  # after close
    assert not cal.is_open(utc(2026, 11, 27, 18, 30))  # after the 13:00 early close
    assert not cal.is_open(utc(2026, 11, 26, 15, 0))  # holiday


def test_next_previous_and_count(cal):
    assert cal.next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    assert cal.previous_trading_day(date(2026, 11, 27)) == date(2026, 11, 25)
    # Nov 23 -> Dec 4: 24, 25, 27, 30, Dec 1, 2, 3, 4 = 8 trading days.
    assert cal.trading_days_between(date(2026, 11, 23), date(2026, 12, 4)) == 8
    assert cal.trading_days_between(date(2026, 12, 4), date(2026, 11, 23)) == 0


TODAY = date(2026, 11, 23)


class Provider:
    def __init__(self, events=None, fail=False):
        self.calls = 0
        self.events = events or {}
        self.fail = fail

    def __call__(self, symbol):
        self.calls += 1
        if self.fail:
            raise ConnectionError("offline")
        return self.events.get(symbol, SymbolEvents())


def events(tmp_path, provider, overrides=None):
    path = tmp_path / "events.json"
    path.write_text(json.dumps(overrides or {}))
    return EventCalendar(
        provider,
        overrides_path=path,
        cache_path=tmp_path / "cache.json",
        clock=lambda: utc(2026, 11, 23, 14, 0),
    )


def test_earnings_before_expiry(tmp_path):
    p = Provider({"AAPL": SymbolEvents(date(2027, 1, 28), None, True)})
    ev = events(tmp_path, p)
    assert ev.earnings_before("AAPL", date(2027, 2, 19), TODAY) is True
    assert ev.earnings_before("AAPL", date(2027, 1, 15), TODAY) is False


def test_unknown_earnings_is_none_not_false(tmp_path):
    ev = events(tmp_path, Provider())
    assert ev.earnings_before("MSFT", date(2027, 1, 15), TODAY) is None


def test_etfs_and_indexes_have_no_earnings(tmp_path):
    p = Provider()
    ev = events(tmp_path, p)
    for sym in ("QQQM", "SPY", "XSP"):
        assert ev.earnings_before(sym, date(2027, 1, 15), TODAY) is False
    assert p.calls == 0


def test_provider_failure_is_unknown_and_not_cached(tmp_path):
    p = Provider(fail=True)
    ev = events(tmp_path, p)
    assert ev.earnings_before("AAPL", date(2027, 1, 15), TODAY) is None
    assert ev.earnings_before("AAPL", date(2027, 1, 15), TODAY) is None
    assert p.calls == 2


def test_cached_once_per_day(tmp_path):
    p = Provider({"AAPL": SymbolEvents(date(2027, 1, 28), None, True)})
    events(tmp_path, p).events("AAPL")
    events(tmp_path, p).events("AAPL")  # new instance reads the cache file
    assert p.calls == 1


def test_overrides_win(tmp_path):
    p = Provider({"AAPL": SymbolEvents(date(2027, 1, 28), None, True)})
    ev = events(
        tmp_path,
        p,
        {
            "aapl": {
                "earnings": "2026-12-10",
                "dividend": {"ex_date": "2026-12-01", "amount": 0.27},
            }
        },
    )
    assert ev.events("AAPL").earnings == date(2026, 12, 10)
    assert ev.ex_dividend_before("AAPL", date(2026, 12, 18), TODAY) == Dividend(
        date(2026, 12, 1), 0.27
    )
    assert p.calls == 0


def test_ex_dividend_window_and_indexes(tmp_path):
    p = Provider({"QQQM": SymbolEvents(None, Dividend(date(2026, 12, 22), 0.33), True)})
    ev = events(tmp_path, p)
    assert ev.ex_dividend_before("QQQM", date(2026, 12, 31), TODAY).amount == 0.33
    assert ev.ex_dividend_before("QQQM", date(2026, 12, 18), TODAY) is None
    assert ev.ex_dividend_before("XSP", date(2026, 12, 31), TODAY) is None


def test_committed_overrides_file_is_valid(tmp_path):
    ev = EventCalendar(
        Provider(),
        overrides_path="data/config/events.json",
        cache_path=tmp_path / "c.json",
    )
    assert ev.events("AAPL") == SymbolEvents()


def test_unknown_result_retried_after_an_hour(tmp_path):
    from datetime import timedelta

    p = Provider()  # answers "unknown" (e.g. rate-limited)
    now = [utc(2026, 11, 23, 14, 0)]
    path = tmp_path / "events.json"
    path.write_text("{}")
    ev = EventCalendar(
        p, overrides_path=path, cache_path=tmp_path / "c.json", clock=lambda: now[0]
    )
    ev.events("AAPL")
    now[0] += timedelta(minutes=30)
    ev.events("AAPL")
    assert p.calls == 1
    now[0] += timedelta(minutes=31)
    ev.events("AAPL")
    assert p.calls == 2
