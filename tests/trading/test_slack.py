"""Slack alerts and Approve / Reject buttons (fake Slack clients, no network)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from src.trading import slack
from src.trading.approvals import ApprovalQueue, ApprovalStatus
from src.trading.config import AlertsConfig
from src.trading.domain import Action, IntentKind, OrderLeg, StockContract, TradeIntent
from src.trading.journal import Journal
from src.trading.notifier import Alert, Notifier, channels_from_env

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)


class FakeWeb:
    def __init__(self):
        self.calls = []

    def chat_postMessage(self, **kw):
        self.calls.append(("post", kw))

    def chat_update(self, **kw):
        self.calls.append(("update", kw))

    def chat_postEphemeral(self, **kw):
        self.calls.append(("ephemeral", kw))


def pending(tmp_path, iid="abc123"):
    j = Journal(tmp_path / "j.db")
    it = TradeIntent(
        iid,
        "rebuild",
        IntentKind.OPEN,
        (OrderLeg(StockContract("SPY"), Action.BUY, 37),),
        "REBUILD",
        NOW,
    )
    ApprovalQueue(j).request(it, "Open (rebuild): buy 37 SPY [REBUILD]")
    j.close()
    return str(tmp_path / "j.db")


def test_approval_alert_gets_buttons_and_others_dont():
    web = FakeWeb()
    ch = slack.SlackChannel("xoxb", "C1", client=web)
    ch.send(Alert("warning", "approval:abc123", "Approval needed", "buy 37 SPY", NOW))
    ch.send(Alert("critical", "margin:breach", "Margin breach", "EL $90k", NOW))
    (_, first), (_, second) = web.calls
    actions = [b for b in first["blocks"] if b["type"] == "actions"][0]["elements"]
    assert [(a["action_id"], a["value"]) for a in actions] == [
        ("approve", "abc123"),
        ("reject", "abc123"),
    ]
    assert "confirm" in actions[0]  # Slack asks "Send this order?" before approving
    assert "blocks" not in second and second["channel"] == "C1"


def test_channel_from_env():
    chans = channels_from_env({"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_CHANNEL": "C1"})
    assert [c.name for c in chans] == ["slack"]
    assert channels_from_env({"SLACK_BOT_TOKEN": "xoxb-1"}) == []


def test_only_listed_users_can_decide(tmp_path):
    path = pending(tmp_path)
    ok, text = slack.decide(
        {"action_id": "approve", "value": "abc123"},
        {"id": "U999", "username": "eve"},
        path,
        {"U1"},
    )
    assert not ok and "isn't on the approver list" in text
    assert ApprovalQueue(Journal(path)).status("abc123") is ApprovalStatus.PENDING


def test_approve_and_reject(tmp_path):
    path = pending(tmp_path)
    ok, text = slack.decide(
        {"action_id": "approve", "value": "abc123"},
        {"id": "U1", "username": "owner"},
        path,
        {"U1"},
    )
    assert ok and "Approved by owner" in text
    q = ApprovalQueue(Journal(path))
    assert q.status("abc123") is ApprovalStatus.APPROVED
    assert q.recent(1)[0]["decided_by"] == "slack:owner"
    ok, text = slack.decide(
        {"action_id": "reject", "value": "abc123"}, {"id": "U1"}, path, {"U1"}
    )
    assert not ok and "already approved" in text  # one decision per request
    (tmp_path / "b").mkdir()
    path2 = pending(tmp_path / "b", "def456")
    ok, text = slack.decide(
        {"action_id": "reject", "value": "def456"}, {"id": "U1"}, path2, {"U1"}
    )
    assert (
        ok and ApprovalQueue(Journal(path2)).status("def456") is ApprovalStatus.REJECTED
    )


def test_button_press_updates_the_message(tmp_path):
    path = pending(tmp_path)
    acked, web = [], FakeWeb()
    client = SimpleNamespace(send_socket_mode_response=acked.append, web_client=web)
    blocks = slack.approval_blocks("Approval needed", "buy 37 SPY", "abc123")
    req = SimpleNamespace(
        envelope_id="env1",
        type="interactive",
        payload={
            "type": "block_actions",
            "user": {"id": "U1", "username": "owner"},
            "channel": {"id": "C1"},
            "message": {"ts": "123.456", "blocks": blocks},
            "actions": [{"action_id": "approve", "value": "abc123"}],
        },
    )
    slack.handle(client, req, path, {"U1"})
    assert acked and acked[0].envelope_id == "env1"
    kind, kw = web.calls[0]
    assert kind == "update" and kw["ts"] == "123.456"
    assert not [b for b in kw["blocks"] if b["type"] == "actions"]  # buttons removed
    assert "Approved by owner" in kw["text"]
    # An outsider gets a private refusal; the message is left alone.
    req.payload["user"] = {"id": "U999"}
    web.calls.clear()
    slack.handle(client, req, path, {"U1"})
    assert web.calls[0][0] == "ephemeral"


def test_notifier_sends_approval_requests_to_slack():
    web = FakeWeb()
    n = Notifier(
        AlertsConfig(),
        channels=[slack.SlackChannel("x", "C1", client=web)],
        clock=lambda: NOW,
    )
    n.notify("warning", "approval:zzz", "Approval needed", "buy 1 XSP put")
    assert web.calls and web.calls[0][1]["blocks"][1]["elements"][0]["value"] == "zzz"


def test_approvers_from_env():
    assert slack.approvers_from_env({"SLACK_APPROVER_IDS": "U1, U2 ,"}) == {"U1", "U2"}
