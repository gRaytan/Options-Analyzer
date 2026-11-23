"""Kill switch and persistent halts (plan T15)."""
import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.domain import (
    Action,
    Fill,
    HaltScope,
    OptionContract,
    Right,
    set_clock,
)
from src.trading.journal import Journal
from src.trading.kill_switch import HaltManager, main

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def mgr(tmp_path):
    journal = Journal(tmp_path / "j.db")
    return HaltManager(journal, tmp_path / "HALT", clock=lambda: T0)


def test_no_halt_by_default(mgr):
    assert not mgr.state().active
    assert mgr.allows_opening() and mgr.allows_closing()


def test_opening_halt_keeps_closes(mgr):
    mgr.halt("testing")
    assert not mgr.allows_opening()
    assert mgr.allows_closing()
    assert mgr.state().set_by == "owner"


def test_full_halt_blocks_everything(mgr):
    mgr.halt("stop", HaltScope.ALL)
    assert not mgr.allows_opening() and not mgr.allows_closing()


def test_halt_never_weakens(mgr):
    mgr.trigger("reconcile mismatch", HaltScope.ALL)
    st = mgr.trigger("engine error")  # an opening-only trigger after a full halt
    assert st.scope is HaltScope.ALL
    assert "reconcile mismatch" in st.reason and "engine error" in st.reason


def test_halt_escalates(mgr):
    mgr.halt("first")
    assert mgr.halt("worse", HaltScope.ALL).scope is HaltScope.ALL


def test_persists_across_restart(tmp_path):
    path = tmp_path / "j.db"
    with Journal(path) as j:
        HaltManager(j, tmp_path / "HALT").trigger("daily loss")
    with Journal(path) as j:
        st = HaltManager(j, tmp_path / "HALT").state()
    assert st.active and st.reason == "daily loss" and st.set_by == "system"


def test_only_humans_resume(mgr):
    mgr.trigger("x")
    with pytest.raises(PermissionError):
        mgr.resume(by="system")
    with pytest.raises(PermissionError):
        mgr.resume(by="")
    mgr.resume(by="owner", note="checked")
    assert mgr.allows_opening()


def test_halt_file(mgr, tmp_path):
    (tmp_path / "HALT").write_text("")
    st = mgr.state()
    assert st.active and st.scope is HaltScope.OPENING and st.set_by == "file"
    (tmp_path / "HALT").write_text("all please")
    assert mgr.state().scope is HaltScope.ALL
    mgr.resume(by="owner")
    assert not (tmp_path / "HALT").exists() and mgr.allows_opening()


def test_file_and_journal_combine_to_stricter(mgr, tmp_path):
    mgr.halt("journal halt")  # opening
    (tmp_path / "HALT").write_text("all")
    assert mgr.state().scope is HaltScope.ALL


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    monkeypatch.setenv("TRADING_HALT_FILE", str(tmp_path / "HALT"))
    main(["halt", "--all", "--reason", "going on holiday"])
    assert "HALTED (all): going on holiday" in capsys.readouterr().out
    main(["status"])
    assert "HALTED" in capsys.readouterr().out
    main(["resume", "--by", "owner"])
    assert "running (no halt)" in capsys.readouterr().out


PUT = OptionContract("XSP", date(2026, 12, 31), 550, Right.PUT)


def test_engine_halts_all_on_reconcile_mismatch(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    journal = Journal(tmp_path / "j.db")
    journal.append("fills", Fill("e1", "r", PUT, Action.SELL, 1, 1.0, T0))
    broker = FakeAdapter(account_id="DU123", read_only=True)
    # The journalled fill is for a contract expiring 2026-12-31; pin the engine
    # clock so reconcile still expects it after that date.
    set_clock(lambda: T0)
    try:
        asyncio.run(
            run.monitor(
                0,
                once=True,
                adapter=broker,
                journal=journal,
                halt_file=tmp_path / "HALT",
            )
        )
    finally:
        set_clock(None)
    beat = json.loads(hb.read_text())
    assert beat["halted"] is True and beat["halt_scope"] == "all"
    assert "reconcile mismatch" in beat["halt_reason"]


def test_engine_error_halts_opening_and_keeps_running(tmp_path, monkeypatch):
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(hb))
    broker = FakeAdapter(account_id="DU123", read_only=True)

    async def boom():
        raise RuntimeError("bug")

    broker.positions = boom  # not a BrokerError: an unexpected failure
    journal = Journal(tmp_path / "j.db")
    code = asyncio.run(
        run.monitor(
            0, once=True, adapter=broker, journal=journal, halt_file=tmp_path / "HALT"
        )
    )
    assert code == 0
    beat = json.loads(hb.read_text())
    assert beat["halted"] is True and beat["halt_scope"] == "opening"
    assert "RuntimeError: bug" in beat["halt_reason"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    monkeypatch.setenv("TRADING_HALT_FILE", str(tmp_path / "HALT"))
    monkeypatch.setenv("TRADING_HEARTBEAT", str(tmp_path / "hb.json"))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), tmp_path


def token(c):
    import re

    html = c.get("/bot").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_web_halt_and_resume(client):
    c, tmp_path = client
    assert "Not halted" in c.get("/bot").get_data(as_text=True)
    t = token(c)
    c.post("/bot/halt", data={"scope": "all", "reason": "news", "csrf_token": t})
    html = c.get("/bot").get_data(as_text=True)
    assert "HALTED: everything" in html and "news" in html
    c.post("/bot/resume", data={"csrf_token": t})  # no confirmation: still halted
    assert "HALTED" in c.get("/bot").get_data(as_text=True)
    c.post("/bot/resume", data={"confirm": "yes", "csrf_token": t})
    assert "Not halted" in c.get("/bot").get_data(as_text=True)
    assert c.get("/api/bot-status").get_json()["halt"]["active"] is False


def test_web_shows_halt_file_without_journal(client):
    c, tmp_path = client
    (tmp_path / "HALT").write_text("")
    assert "HALTED: no new trades" in c.get("/bot").get_data(as_text=True)


def test_web_resume_without_csrf_token_is_refused(client):
    c, tmp_path = client
    t = token(c)
    c.post("/bot/halt", data={"scope": "all", "csrf_token": t})
    # A forged cross-site form has no token (or a wrong one).
    c.post("/bot/resume", data={"confirm": "yes"})
    c.post("/bot/resume", data={"confirm": "yes", "csrf_token": "forged"})
    assert "HALTED" in c.get("/bot").get_data(as_text=True)
