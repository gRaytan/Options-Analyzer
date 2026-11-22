"""Alerts to email and phone (plan T20, spec §9.3).

Channels are configured from the environment (never from committed files):

* **Email** (SMTP): ``ALERT_SMTP_HOST``, ``ALERT_SMTP_PORT`` (587, STARTTLS),
  ``ALERT_SMTP_USER``, ``ALERT_SMTP_PASSWORD``, ``ALERT_EMAIL_FROM``,
  ``ALERT_EMAIL_TO``.
* **Telegram** (push to your phone): ``ALERT_TELEGRAM_TOKEN`` (from
  @BotFather) and ``ALERT_TELEGRAM_CHAT_ID``.
* **Log**: always on.

Rules:

* Levels: ``info`` < ``warning`` < ``critical``. Alerts below
  ``alerts.min_level`` go to the log only.
* The same alert key repeats at most every ``repeat_minutes``, unless its
  level rises (a warning followed by a critical always goes out).
* At most ``max_per_hour`` alerts per hour in total (critical ones always pass).
* Sending runs in a worker thread with a timeout, and a failing channel is
  logged and skipped. **Alerts can never stop or slow the engine.**
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Callable, Deque, Dict, List, Optional, Protocol, Tuple

from src.trading.config import AlertsConfig
from src.trading.domain import utcnow

log = logging.getLogger("trading.alerts")

LEVELS = {"info": 0, "warning": 1, "critical": 2}
SEND_TIMEOUT_S = 10


@dataclass(frozen=True)
class Alert:
    level: str
    key: str  # identifies "the same alert" for repeat limiting
    title: str
    message: str
    ts: datetime

    @property
    def subject(self) -> str:
        tag = {"info": "", "warning": "[WARNING] ", "critical": "[CRITICAL] "}[
            self.level
        ]
        return f"{tag}Options bot: {self.title}"


class Channel(Protocol):
    name: str

    def send(self, alert: Alert) -> None:
        ...


class EmailChannel:
    name = "email"

    def __init__(
        self, host, port, user, password, sender, recipients, smtp_factory=smtplib.SMTP
    ):
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.sender, self.recipients = sender, recipients
        self._smtp = smtp_factory

    def build(self, alert: Alert) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = alert.subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg.set_content(f"{alert.message}\n\n{alert.ts.isoformat()} ({alert.level})")
        return msg

    def send(self, alert: Alert) -> None:
        with self._smtp(self.host, self.port, timeout=SEND_TIMEOUT_S) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(self.build(alert))


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, opener=urllib.request.urlopen):
        self.token, self.chat_id = token, chat_id
        self._open = opener

    def build(self, alert: Alert) -> Tuple[str, bytes]:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps(
            {"chat_id": self.chat_id, "text": f"{alert.subject}\n\n{alert.message}"}
        ).encode()
        return url, body

    def send(self, alert: Alert) -> None:
        url, body = self.build(alert)
        req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        with self._open(req, timeout=SEND_TIMEOUT_S) as resp:
            if getattr(resp, "status", 200) >= 300:
                raise RuntimeError(f"telegram returned HTTP {resp.status}")


def channels_from_env(env: Optional[Dict[str, str]] = None) -> List[Channel]:
    env = os.environ if env is None else env
    out: List[Channel] = []
    if env.get("ALERT_SMTP_HOST") and env.get("ALERT_EMAIL_TO"):
        out.append(
            EmailChannel(
                env["ALERT_SMTP_HOST"],
                env.get("ALERT_SMTP_PORT", "587"),
                env.get("ALERT_SMTP_USER", ""),
                env.get("ALERT_SMTP_PASSWORD", ""),
                env.get("ALERT_EMAIL_FROM", env.get("ALERT_SMTP_USER", "")),
                [a.strip() for a in env["ALERT_EMAIL_TO"].split(",") if a.strip()],
            )
        )
    if env.get("ALERT_TELEGRAM_TOKEN") and env.get("ALERT_TELEGRAM_CHAT_ID"):
        out.append(
            TelegramChannel(env["ALERT_TELEGRAM_TOKEN"], env["ALERT_TELEGRAM_CHAT_ID"])
        )
    return out


class Notifier:
    def __init__(
        self,
        config: AlertsConfig = AlertsConfig(),
        channels: Optional[List[Channel]] = None,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.config = config
        self.channels = channels if channels is not None else channels_from_env()
        self._clock = clock
        self._last: Dict[str, Tuple[datetime, int]] = {}  # key -> (sent at, level)
        self._sent: Deque[datetime] = deque()
        self.sent_log: List[Alert] = []  # what was actually sent (for tests / status)

    def _should_send(self, alert: Alert) -> bool:
        level = LEVELS[alert.level]
        if level < LEVELS[self.config.min_level]:
            return False
        prev = self._last.get(alert.key)
        repeat = timedelta(minutes=self.config.repeat_minutes)
        if prev and alert.ts - prev[0] < repeat and level <= prev[1]:
            return False
        while self._sent and alert.ts - self._sent[0] > timedelta(hours=1):
            self._sent.popleft()
        if len(self._sent) >= self.config.max_per_hour and alert.level != "critical":
            return False
        return True

    def notify(self, level: str, key: str, title: str, message: str) -> Optional[Alert]:
        """Decide and send synchronously (used by tests and non-async callers)."""
        alert = self._prepare(level, key, title, message)
        if alert is not None:
            self._deliver(alert)
        return alert

    async def anotify(
        self, level: str, key: str, title: str, message: str
    ) -> Optional[Alert]:
        """Engine-side: send in a worker thread so the event loop never blocks."""
        import asyncio  # pylint: disable=import-outside-toplevel

        alert = self._prepare(level, key, title, message)
        if alert is not None:
            await asyncio.to_thread(self._deliver, alert)
        return alert

    def _prepare(self, level, key, title, message) -> Optional[Alert]:
        if level not in LEVELS:
            raise ValueError(f"unknown alert level {level!r}")
        alert = Alert(level, key, title, message, self._clock())
        log_fn = {"info": log.info, "warning": log.warning, "critical": log.error}[
            level
        ]
        log_fn("ALERT %s: %s", title, message)
        if not self._should_send(alert):
            return None
        self._last[key] = (alert.ts, LEVELS[level])
        self._sent.append(alert.ts)
        self.sent_log.append(alert)
        return alert

    def _deliver(self, alert: Alert) -> None:
        for channel in self.channels:
            try:
                channel.send(alert)
            except Exception as exc:  # pylint: disable=broad-except
                log.warning("alert channel %s failed: %s", channel.name, exc)
