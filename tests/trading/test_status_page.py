"""Bot status page (plan T12)."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.config import MarginFloorConfig
from src.trading.domain import (
    AccountSnapshot,
    OptionContract,
    Position,
    Right,
    StockContract,
    set_clock,
)
from src.trading.journal import Journal
from src.trading.status import (
    MarginState,
    classify_margin,
    load_bot_status,
    write_heartbeat,
)

NOW = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
FLOOR = MarginFloorConfig()


def snap(el, nlv=1_000_000, ts=NOW):
    return AccountSnapshot("DU123", ts, nlv, el, 300_000, 380_000, 420_000, -5000)


@pytest.mark.parametrize(
    "el,nlv,state",
    [
        (450_000, 1_000_000, MarginState.NORMAL),
        (120_000, 250_000, MarginState.WARNING),  # EL below $125k entry level
        (350_000, 1_000_000, MarginState.WARNING),  # cushion 35% < 40%
        (99_000, 200_000, MarginState.BREACH),  # EL below $100k
        (250_000, 1_000_000, MarginState.BREACH),  # cushion 25% < 30%
        (49_000, 100_000, MarginState.EMERGENCY),  # EL below $50k
        (140_000, 1_000_000, MarginState.EMERGENCY),  # cushion 14% < 15%
    ],
)
def test_classify_margin(el, nlv, state):
    assert classify_margin(snap(el, nlv), FLOOR) is state


def test_classify_unknown():
    assert classify_margin(None, FLOOR) is MarginState.UNKNOWN
    assert classify_margin(snap(0, nlv=0), FLOOR) is MarginState.UNKNOWN


def test_status_when_engine_never_started(tmp_path):
    status = load_bot_status(tmp_path / "none.db", tmp_path / "none.json", FLOOR, NOW)
    assert not status.engine_running and status.heartbeat_age_s is None
    assert status.account is None and status.margin_state is MarginState.UNKNOWN
    assert not (tmp_path / "none.db").exists()  # the UI doesn't create a journal


def populated(tmp_path, heartbeat_age=timedelta(seconds=20), connected=True):
    journal = Journal(tmp_path / "j.db")
    journal.append(
        "account_snapshot", snap(450_000, ts=NOW - timedelta(minutes=2)), key="DU123"
    )
    positions = (
        Position(StockContract("QQQM"), 300, 180.0, 210.0),
        Position(
            OptionContract("QQQM", date(2026, 12, 18), 240, Right.CALL), -1, 1.25, 0.8
        ),
    )
    journal.append("positions_snapshot", positions, key="DU123", ts=NOW)
    journal.close()
    hb = tmp_path / "hb.json"
    hb.write_text(
        json.dumps(
            {
                "ts": (NOW - heartbeat_age).isoformat(),
                "connected": connected,
                "read_only": True,
                "live": False,
                "account_stale": False,
                "last_error": "",
            }
        )
    )
    return tmp_path / "j.db", hb


def test_status_running(tmp_path):
    status = load_bot_status(*populated(tmp_path), FLOOR, NOW)
    assert status.engine_running and status.connected and status.read_only
    assert status.account.excess_liquidity == 450_000
    assert status.account_age_s == 120
    assert status.margin_state is MarginState.NORMAL
    assert [p.contract.key for p in status.positions] == [
        "QQQM",
        "QQQM 2026-12-18 C 240",
    ]
    data = status.to_json()
    assert data["margin_state"] == "normal" and len(data["positions"]) == 2


def test_status_engine_down_shows_disconnected(tmp_path):
    paths = populated(tmp_path, heartbeat_age=timedelta(minutes=10), connected=True)
    status = load_bot_status(*paths, FLOOR, NOW)
    assert not status.engine_running
    assert not status.connected  # a dead engine's last "connected" isn't trusted
    assert status.account is not None  # last known numbers still shown


def test_write_heartbeat_roundtrip(tmp_path):
    path = tmp_path / "sub" / "hb.json"
    write_heartbeat(path, connected=True, read_only=True)
    data = json.loads(path.read_text())
    assert data["connected"] is True and "ts" in data


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.web.app import create_app

    journal, hb = populated(tmp_path)
    monkeypatch.setenv("TRADING_JOURNAL", str(journal))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(hb))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_bot_page_renders(client):
    # Heartbeat timestamps are relative to NOW (a fixed date), so the engine
    # reads as down in real time; the page must still render the last data.
    resp = client.get("/bot")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Trading Bot" in html
    assert "$450,000" in html
    assert "QQQM 2026-12-18 C 240" in html
    assert "Assignable early" in html


def test_bot_page_renders_with_nothing(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "missing.db"))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(tmp_path / "missing.json"))
    resp = create_app().test_client().get("/bot")
    assert resp.status_code == 200
    assert "No account data yet" in resp.get_data(as_text=True)


def test_bot_status_api(client):
    # The route dates the snapshot against the engine clock; pin it to NOW so
    # the fixture's 2-minute-old account data stays inside the 240 s window.
    set_clock(lambda: NOW)
    try:
        data = client.get("/api/bot-status").get_json()
    finally:
        set_clock(None)
    assert data["account"]["excess_liquidity"] == 450_000
    assert data["margin_state"] == "normal"


def test_nav_has_bot_link(client):
    assert 'href="/bot"' in client.get("/bot").get_data(as_text=True)


def test_bot_page_shows_reconcile_mismatch(tmp_path, monkeypatch):
    from src.web.app import create_app

    journal_path, hb = populated(tmp_path)
    j = Journal(journal_path)
    j.append(
        "reconciliations",
        {
            "ok": False,
            "matched": [],
            "unmanaged": ["QQQM"],
            "expired": [],
            "mismatches": [
                {
                    "key": "XSP 2026-12-31 P 550",
                    "expected": -1,
                    "actual": 0,
                    "hint": "gone",
                }
            ],
        },
    )
    j.close()
    monkeypatch.setenv("TRADING_JOURNAL", str(journal_path))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(hb))
    html = create_app().test_client().get("/bot").get_data(as_text=True)
    assert "Reconciliation mismatch" in html and "XSP 2026-12-31 P 550" in html


def test_old_account_data_is_at_least_warning(tmp_path):
    paths = populated(tmp_path)  # snapshot is 2 min old: fresh
    assert load_bot_status(*paths, FLOOR, NOW).margin_state is MarginState.NORMAL
    later = NOW + timedelta(minutes=10)
    status = load_bot_status(*paths, FLOOR, later)
    assert status.margin_state is MarginState.WARNING and status.account_stale
