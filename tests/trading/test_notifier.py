"""Alerts (plan T20)."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.trading import run
from src.trading.broker.fake import FakeAdapter
from src.trading.config import AlertsConfig
from src.trading.domain import Action, Fill, OptionContract, Right
from src.trading.journal import Journal
from src.trading.notifier import (
    Alert,
    EmailChannel,
    Notifier,
    TelegramChannel,
    channels_from_env,
)

T0 = datetime(2026, 11, 23, 15, 0, tzinfo=timezone.utc)


class Recorder:
    name = "rec"

    def __init__(self, fail=False):
        self.alerts = []
        self.fail = fail

    def send(self, alert):
        if self.fail:
            raise ConnectionError("down")
        self.alerts.append(alert)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def make(**cfg):
    rec, clock = Recorder(), Clock()
    return Notifier(AlertsConfig(**cfg), [rec], clock), rec, clock


def test_level_filter():
    n, rec, _ = make(min_level="warning")
    n.notify("info", "a", "t", "m")
    n.notify("warning", "b", "t", "m")
    assert [a.key for a in rec.alerts] == ["b"]


def test_repeat_limited_unless_escalated():
    n, rec, clock = make(repeat_minutes=15)
    n.notify("warning", "margin", "t", "m")
    clock.now += timedelta(minutes=5)
    n.notify("warning", "margin", "t", "m")  # suppressed
    n.notify("critical", "margin", "t", "m")  # escalation goes out
    clock.now += timedelta(minutes=16)
    n.notify("critical", "margin", "t", "m")  # after the window
    assert [a.level for a in rec.alerts] == ["warning", "critical", "critical"]


def test_hourly_cap_spares_critical():
    n, rec, clock = make(max_per_hour=2, repeat_minutes=0)
    for i in range(4):
        n.notify("warning", f"k{i}", "t", "m")
    n.notify("critical", "c", "t", "m")
    assert [a.key for a in rec.alerts] == ["k0", "k1", "c"]
    clock.now += timedelta(hours=1, seconds=1)
    n.notify("warning", "k9", "t", "m")
    assert rec.alerts[-1].key == "k9"


def test_failing_channel_does_not_raise_or_block_others():
    bad, good = Recorder(fail=True), Recorder()
    n = Notifier(AlertsConfig(), [bad, good], Clock())
    n.notify("critical", "x", "t", "m")
    assert len(good.alerts) == 1


def test_unknown_level_rejected():
    n, _, _ = make()
    with pytest.raises(ValueError):
        n.notify("panic", "x", "t", "m")


def test_email_message_and_smtp_flow():
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, msg):
            calls.append(("send", msg["Subject"], msg["To"]))

    ch = EmailChannel(
        "smtp.x", 587, "me", "pw", "me@x", ["a@x", "b@x"], smtp_factory=FakeSMTP
    )
    ch.send(Alert("critical", "k", "Margin breach", "EL $95,000", T0))
    assert calls == [
        ("connect", "smtp.x", 587, 10),
        ("starttls",),
        ("login", "me"),
        ("send", "[CRITICAL] Options bot: Margin breach", "a@x, b@x"),
    ]


def test_telegram_request():
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen["url"], seen["body"], seen["timeout"] = req.full_url, req.data, timeout
        return Resp()

    TelegramChannel("TOKEN", "42", opener).send(
        Alert("warning", "k", "Cannot reach IB", "m", T0)
    )
    assert seen["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    body = json.loads(seen["body"])
    assert body["chat_id"] == "42" and body["text"].startswith(
        "[WARNING] Options bot: Cannot reach IB"
    )


def test_channels_from_env():
    assert channels_from_env({}) == []
    chans = channels_from_env(
        {
            "ALERT_SMTP_HOST": "smtp.x",
            "ALERT_EMAIL_TO": "a@x, b@x",
            "ALERT_TELEGRAM_TOKEN": "t",
            "ALERT_TELEGRAM_CHAT_ID": "1",
        }
    )
    assert [c.name for c in chans] == ["email", "telegram"]
    assert chans[0].recipients == ["a@x", "b@x"]


PUT = OptionContract("XSP", date(2026, 12, 31), 600, Right.PUT)


def run_once(tmp_path, monkeypatch, broker, notifier):
    monkeypatch.setattr(run, "HEARTBEAT_FILE", str(tmp_path / "hb.json"))
    asyncio.run(
        run.monitor(
            0,
            once=True,
            adapter=broker,
            journal=Journal(tmp_path / "j.db"),
            halt_file=tmp_path / "H",
            notifier=notifier,
        )
    )


def test_engine_alerts_on_breach_and_halt(tmp_path, monkeypatch):
    rec = Recorder()
    notifier = Notifier(AlertsConfig(), [rec])
    broker = FakeAdapter(account_id="DU123", read_only=True)
    broker.set_account(excess_liquidity=95_000, net_liquidation=200_000)
    j = Journal(tmp_path / "j.db")
    j.append("fills", Fill("e", "r", PUT, Action.SELL, 1, 1.0, T0))  # IB won't show it
    run_once(tmp_path, monkeypatch, broker, notifier)
    titles = {a.title for a in rec.alerts}
    assert "Margin breach" in titles and "Bot halted (everything)" in titles
    # The breaching account also fails the -30% crash test (a warning, by design).
    assert all(a.level == "critical" for a in rec.alerts if a.key != "crash-test")


def test_engine_no_alert_on_normal_start(tmp_path, monkeypatch):
    rec = Recorder()
    run_once(
        tmp_path,
        monkeypatch,
        FakeAdapter(account_id="DU123", read_only=True),
        Notifier(AlertsConfig(min_level="info"), [rec]),
    )
    assert rec.alerts == []
