"""Reconciliation (plan T13)."""
import asyncio
from datetime import date, datetime, timezone

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.domain import (
    Action,
    Fill,
    OptionContract,
    Position,
    Quote,
    Right,
    StockContract,
)
from src.trading.journal import MIGRATIONS, Journal
from src.trading.reconcile import (
    expected_from_fills,
    expected_from_journal,
    reconcile,
    result_to_dict,
)

T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)
PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)
OLD_PUT = OptionContract("XSP", date(2026, 11, 20), 540, Right.PUT)
CALL = OptionContract("QQQM", date(2026, 12, 18), 270, Right.CALL)
QQQM = StockContract("QQQM")


def fill(contract, action, qty, n=[0]):
    n[0] += 1
    return Fill(f"e{n[0]}", "ref", contract, action, qty, 1.0, T0)


def test_expected_nets_fills_and_drops_flat():
    exp = expected_from_fills(
        [
            fill(PUT, Action.SELL, 3),
            fill(PUT, Action.BUY, 1),
            fill(CALL, Action.SELL, 1),
            fill(CALL, Action.BUY, 1),
        ]
    )
    assert {k: q for k, (_, q) in exp.items()} == {PUT.key: -2}


def test_all_match_and_owner_positions_unmanaged():
    exp = expected_from_fills([fill(PUT, Action.SELL, 2)])
    actual = [
        Position(PUT, -2, 1.0),
        Position(QQQM, 600, 180.0),
        Position(CALL, -1, 1.1),
    ]
    result = reconcile(exp, actual, TODAY)
    assert result.ok and not result.should_halt
    assert result.matched == (PUT.key,)
    assert result.unmanaged == (QQQM.key, CALL.key)


def test_quantity_drift_is_mismatch():
    exp = expected_from_fills([fill(PUT, Action.SELL, 2)])
    result = reconcile(exp, [Position(PUT, -3, 1.0)], TODAY)
    assert result.should_halt
    [m] = result.mismatches
    assert (m.expected, m.actual) == (-2, -3)
    assert "differs" in m.hint


def test_missing_short_option_before_expiry_hints_assignment():
    exp = expected_from_fills([fill(CALL, Action.SELL, 1)])
    [m] = reconcile(exp, [], TODAY).mismatches
    assert m.actual == 0 and "assignment" in m.hint


def test_expired_option_is_explained():
    exp = expected_from_fills([fill(OLD_PUT, Action.SELL, 1)])
    result = reconcile(exp, [], TODAY)
    assert result.ok and result.expired == (OLD_PUT.key,)


def test_long_option_missing_is_mismatch():
    exp = expected_from_fills([fill(PUT, Action.BUY, 1)])
    [m] = reconcile(exp, [], TODAY).mismatches
    assert m.hint == "position missing at IB"


def test_bot_shares_must_remain_but_owner_extra_is_fine():
    exp = expected_from_fills([fill(QQQM, Action.BUY, 100)])
    assert reconcile(exp, [Position(QQQM, 700, 1.0)], TODAY).ok
    result = reconcile(exp, [Position(QQQM, 50, 1.0)], TODAY)
    assert result.should_halt and "shares" in result.mismatches[0].hint


def test_result_to_dict_and_journal_migration(tmp_path):
    journal = Journal(tmp_path / "j.db")
    assert journal.schema_version == len(MIGRATIONS) == 2
    journal.append("fills", fill(PUT, Action.SELL, 1))
    exp = expected_from_journal(journal)
    summary = result_to_dict(reconcile(exp, [], TODAY))
    journal.append("reconciliations", summary)
    assert journal.latest("reconciliations").data["mismatches"][0]["key"] == PUT.key


def test_v1_journal_upgrades_to_v2(tmp_path):
    import sqlite3

    from src.trading import journal as journal_mod

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for stmt in journal_mod.MIGRATIONS[0]:
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_version VALUES (1)")
    conn.commit()
    conn.close()
    with Journal(path) as j:
        assert j.schema_version == 2
        j.append("reconciliations", {"ok": True})


def test_monitor_reconciles_and_reports_in_heartbeat(tmp_path, monkeypatch):
    import json

    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = FakeAdapter(account_id="DU123", read_only=True)
    broker.set_position(QQQM, 600, 180.0)
    journal = Journal(tmp_path / "j.db")
    journal.append("fills", fill(PUT, Action.SELL, 1))  # bot sold a put IB doesn't show
    asyncio.run(run.monitor(0, once=True, adapter=broker, journal=journal))
    beat = json.loads(hb.read_text())
    assert (
        beat["reconcile_ok"] is False
        and beat["mismatches"] == 1
        and beat["unmanaged"] == 1
    )
    rec = Journal(tmp_path / "j.db").latest("reconciliations")
    assert rec.data["unmanaged"] == ["QQQM"]
