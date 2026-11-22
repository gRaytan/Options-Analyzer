"""Approval queue (plan T27)."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading.approvals import ApprovalError, ApprovalQueue, ApprovalStatus
from src.trading.domain import (
    Action,
    IntentKind,
    OptionContract,
    OrderLeg,
    Right,
    TradeIntent,
)
from src.trading.journal import Journal

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)
PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT)


def intent(iid="i-1"):
    return TradeIntent(
        iid, "index_put", IntentKind.OPEN, (OrderLeg(PUT, Action.SELL, 2),), "SCAN", T0
    )


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def queue(tmp_path):
    clock = Clock()
    alerts = []
    q = ApprovalQueue(Journal(tmp_path / "j.db"), clock=clock, on_request=alerts.append)
    q.clock, q.alerts = clock, alerts
    return q


def test_request_approve(queue):
    queue.request(intent(), "Sell 2 XSP 600P Dec 31 @ 1.05")
    assert queue.status("i-1") is ApprovalStatus.PENDING
    assert len(queue.alerts) == 1 and [p["intent_id"] for p in queue.pending()] == [
        "i-1"
    ]
    queue.approve("i-1", by="owner", note="ok")
    assert queue.status("i-1") is ApprovalStatus.APPROVED
    assert queue.pending() == []


def test_reject(queue):
    queue.request(intent(), "x")
    queue.reject("i-1", by="owner")
    assert queue.status("i-1") is ApprovalStatus.REJECTED
    with pytest.raises(ApprovalError, match="already rejected"):
        queue.approve("i-1", by="owner")


def test_expires_after_ten_minutes(queue):
    queue.request(intent(), "x")
    queue.clock.now += timedelta(minutes=10)
    assert queue.status("i-1") is ApprovalStatus.EXPIRED
    assert queue.pending() == []
    with pytest.raises(ApprovalError, match="expired"):
        queue.approve("i-1", by="owner")
    assert queue.status("i-1") is ApprovalStatus.EXPIRED  # never becomes approved


def test_request_is_idempotent_and_can_be_renewed_after_expiry(queue):
    queue.request(intent(), "x")
    queue.request(intent(), "x")
    assert len(queue.alerts) == 1
    queue.clock.now += timedelta(minutes=11)
    queue.request(intent(), "x")  # expired: a fresh request
    assert len(queue.alerts) == 2 and queue.status("i-1") is ApprovalStatus.PENDING


def test_decisions_need_a_name_and_a_request(queue):
    queue.request(intent(), "x")
    with pytest.raises(ApprovalError):
        queue.approve("i-1", by="")
    with pytest.raises(ApprovalError):
        queue.approve("nope", by="owner")
    assert queue.status("nope") is ApprovalStatus.UNKNOWN


def test_recent_history(queue):
    queue.request(intent("a"), "A")
    queue.request(intent("b"), "B")
    queue.approve("a", by="owner")
    statuses = {r["intent_id"]: r["status"] for r in queue.recent()}
    assert statuses == {"a": "approved", "b": "pending"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "j.db"))
    q = ApprovalQueue(Journal(tmp_path / "j.db"))
    q.request(intent(), "Sell 2 XSP 600P Dec 31 @ 1.05")
    return create_app().test_client(), tmp_path


def token(c):
    return re.search(
        r'name="csrf_token" value="([^"]+)"',
        c.get("/bot/approvals").get_data(as_text=True),
    ).group(1)


def test_page_lists_and_approves(client):
    c, tmp_path = client
    html = c.get("/bot/approvals").get_data(as_text=True)
    assert "Sell 2 XSP 600P" in html and "Approve" in html
    c.post("/bot/approvals/i-1", data={"decision": "approve", "csrf_token": token(c)})
    assert (
        ApprovalQueue(Journal(tmp_path / "j.db")).status("i-1")
        is ApprovalStatus.APPROVED
    )
    assert "No trades waiting" in c.get("/bot/approvals").get_data(as_text=True)


def test_page_refuses_without_csrf(client):
    c, tmp_path = client
    c.post("/bot/approvals/i-1", data={"decision": "approve"})
    assert (
        ApprovalQueue(Journal(tmp_path / "j.db")).status("i-1")
        is ApprovalStatus.PENDING
    )


def test_page_without_journal(tmp_path, monkeypatch):
    from src.web.app import create_app

    monkeypatch.setenv("TRADING_JOURNAL", str(tmp_path / "none.db"))
    html = create_app().test_client().get("/bot/approvals").get_data(as_text=True)
    assert "No trades waiting" in html and not (tmp_path / "none.db").exists()
