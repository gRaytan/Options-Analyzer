"""Slack: alerts, and Approve / Reject buttons for every approval request.

Uses a Slack app in **Socket Mode**: the bot opens an outbound connection
to Slack, so nothing on the server is exposed to the internet (the web UI
stays on localhost / VPN).

* **Alerts** (``SlackChannel``): every alert the notifier sends (margin,
  halts, crash test, daily report, opportunities...) goes to one channel.
  An approval request ("Approval needed", key ``approval:<intent id>``) gets
  **Approve** and **Reject** buttons.
* **Buttons** (``python -m src.trading.slack``, the ``slack-approver``
  service): a button press approves or rejects that request in the bot's
  approval queue, exactly like the web Approvals page, then the message is
  updated with who decided. **Only the Slack users listed in
  ``SLACK_APPROVER_IDS`` can decide**; anyone else gets a private refusal.
  An approved order still goes through the risk gate before IB.

Environment (never committed):

* ``SLACK_BOT_TOKEN`` (``xoxb-...``) and ``SLACK_CHANNEL`` (channel ID, e.g. ``C0123...``)
* ``SLACK_APP_TOKEN`` (``xapp-...``, Socket Mode, scope ``connections:write``)
* ``SLACK_APPROVER_IDS``: comma-separated Slack user IDs (e.g. ``U0123ABC``)

Set-up steps: deploy/README.md, "Slack".
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Set

from src.trading.approvals import ApprovalError, ApprovalQueue
from src.trading.journal import DEFAULT_JOURNAL_PATH, Journal

log = logging.getLogger("trading.slack")

APPROVE, REJECT = "approve", "reject"
APPROVAL_PREFIX = "approval:"


def approval_blocks(title: str, message: str, intent_id: str) -> List[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{title}*\n{message}"},
        },
        {
            "type": "actions",
            "block_id": f"approval:{intent_id}",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": APPROVE,
                    "value": intent_id,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Send this order?"},
                        "text": {"type": "plain_text", "text": message[:280]},
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "action_id": REJECT,
                    "value": intent_id,
                },
            ],
        },
    ]


class SlackChannel:
    """Notifier channel: posts alerts; approval requests get buttons."""

    name = "slack"

    def __init__(self, token: str, channel: str, client=None):
        if client is None:
            from slack_sdk import WebClient  # pylint: disable=import-outside-toplevel

            client = WebClient(token=token, timeout=10)
        self.client = client
        self.channel = channel

    def build(self, alert) -> dict:
        text = f"{alert.subject}\n{alert.message}"
        payload = {"channel": self.channel, "text": text}
        if alert.key.startswith(APPROVAL_PREFIX):
            intent_id = alert.key[len(APPROVAL_PREFIX) :]
            payload["blocks"] = approval_blocks(alert.title, alert.message, intent_id)
        return payload

    def send(self, alert) -> None:
        self.client.chat_postMessage(**self.build(alert))


def approvers_from_env(env: Optional[Dict[str, str]] = None) -> Set[str]:
    env = os.environ if env is None else env
    return {
        u.strip() for u in env.get("SLACK_APPROVER_IDS", "").split(",") if u.strip()
    }


def decide(
    action: dict,
    user: dict,
    journal_path: str,
    approvers: Set[str],
) -> tuple:
    """Apply one button press. Returns (ok, text to show)."""
    user_id = user.get("id", "")
    who = user.get("username") or user.get("name") or user_id
    if user_id not in approvers:
        return (
            False,
            f"Not allowed: {who} isn't on the approver list (SLACK_APPROVER_IDS).",
        )
    intent_id = action.get("value", "")
    decision = action.get("action_id")
    with Journal(journal_path) as journal:
        queue = ApprovalQueue(journal)
        try:
            if decision == APPROVE:
                queue.approve(intent_id, by=f"slack:{who}")
                return (
                    True,
                    f":white_check_mark: Approved by {who}. The bot sends the order on its next pass (the risk gate still checks it).",
                )
            if decision == REJECT:
                queue.reject(intent_id, by=f"slack:{who}")
                return True, f":x: Rejected by {who}. Nothing will be sent."
        except ApprovalError as exc:
            return False, f"Not done: {exc}"
    return False, "Unknown action."


def handle(client, req, journal_path: str, approvers: Set[str], web=None) -> None:
    """Socket Mode request handler: acknowledge, decide, update the message."""
    from slack_sdk.socket_mode.response import (  # pylint: disable=import-outside-toplevel
        SocketModeResponse,
    )

    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type != "interactive":
        return
    payload = req.payload
    if payload.get("type") != "block_actions":
        return
    web = web or client.web_client
    channel = (payload.get("channel") or {}).get("id")
    message = payload.get("message") or {}
    for action in payload.get("actions", []):
        ok, text = decide(action, payload.get("user", {}), journal_path, approvers)
        if ok:
            blocks = [
                b for b in message.get("blocks", []) if b.get("type") != "actions"
            ]
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
            )
            web.chat_update(
                channel=channel, ts=message.get("ts"), text=text, blocks=blocks
            )
        else:
            web.chat_postEphemeral(
                channel=channel, user=payload["user"]["id"], text=text
            )


def main() -> None:  # pragma: no cover - process entry point
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    approvers = approvers_from_env()
    journal_path = os.environ.get("TRADING_JOURNAL", str(DEFAULT_JOURNAL_PATH))
    if not (app_token and bot_token and approvers):
        log.warning(
            "Slack approvals off: set SLACK_APP_TOKEN, SLACK_BOT_TOKEN and SLACK_APPROVER_IDS"
        )
        while True:  # idle, so the service doesn't restart-loop
            time.sleep(3600)
    from slack_sdk import WebClient  # pylint: disable=import-outside-toplevel
    from slack_sdk.socket_mode import (
        SocketModeClient,
    )  # pylint: disable=import-outside-toplevel

    client = SocketModeClient(
        app_token=app_token, web_client=WebClient(token=bot_token)
    )
    client.socket_mode_request_listeners.append(
        lambda c, r: handle(c, r, journal_path, approvers)
    )
    client.connect()
    log.info("Slack approvals on for %d approver(s)", len(approvers))
    while True:
        time.sleep(60)


if __name__ == "__main__":  # pragma: no cover
    main()
