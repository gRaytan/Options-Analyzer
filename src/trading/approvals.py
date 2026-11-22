"""Approval queue for Stage 3, "approve each" (plan T27, spec §11).

In ``approve`` mode every opening trade waits for the owner's approval:

* The engine calls :meth:`ApprovalQueue.request` with the intent and a
  plain-language summary. An alert goes out; the request **expires after
  10 minutes** (market conditions change).
* The owner approves or rejects on the Approvals page (or the CLI).
* The engine polls :meth:`ApprovalQueue.status` and only sends the order once
  it reads ``approved``. Expired or rejected intents are never sent.

Everything is journaled in ``approvals`` (append-only): the latest record
per intent ID is its current status.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, List, Optional

from src.trading.domain import TradeIntent, to_dict, utcnow
from src.trading.journal import Journal

DEFAULT_TTL = timedelta(minutes=10)


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ApprovalError(Exception):
    pass


class ApprovalQueue:
    def __init__(
        self,
        journal: Journal,
        ttl: timedelta = DEFAULT_TTL,
        clock: Callable[[], datetime] = utcnow,
        on_request: Optional[Callable[[dict], None]] = None,
    ):
        self.journal = journal
        self.ttl = ttl
        self._clock = clock
        self.on_request = on_request  # e.g. send an alert

    def _latest(self, intent_id: str) -> Optional[dict]:
        rec = self.journal.latest("approvals", key=intent_id)
        return rec.data if rec else None

    def _record(self, intent_id: str, data: dict) -> dict:
        self.journal.append("approvals", data, key=intent_id, ts=self._clock())
        return data

    def request(self, intent: TradeIntent, summary: str) -> dict:
        """Ask for approval (idempotent: an open request is returned as is)."""
        current = self._latest(intent.intent_id)
        if current and self._effective_status(current) in (
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
        ):
            return current
        now = self._clock()
        data = {
            "status": ApprovalStatus.PENDING.value,
            "intent_id": intent.intent_id,
            "intent": to_dict(intent),
            "summary": summary,
            "requested_at": now.isoformat(),
            "expires_at": (now + self.ttl).isoformat(),
            "decided_by": "",
        }
        self._record(intent.intent_id, data)
        if self.on_request:
            self.on_request(data)
        return data

    def _effective_status(self, data: dict) -> ApprovalStatus:
        status = ApprovalStatus(data["status"])
        if status is ApprovalStatus.PENDING and self._clock() >= datetime.fromisoformat(
            data["expires_at"]
        ):
            return ApprovalStatus.EXPIRED
        return status

    def status(self, intent_id: str) -> ApprovalStatus:
        current = self._latest(intent_id)
        return self._effective_status(current) if current else ApprovalStatus.UNKNOWN

    def _decide(
        self, intent_id: str, status: ApprovalStatus, by: str, note: str
    ) -> dict:
        if not by:
            raise ApprovalError("a decision needs the name of who made it")
        current = self._latest(intent_id)
        if current is None:
            raise ApprovalError(f"no approval request for {intent_id}")
        effective = self._effective_status(current)
        if effective is ApprovalStatus.EXPIRED:
            self._record(intent_id, {**current, "status": ApprovalStatus.EXPIRED.value})
            raise ApprovalError(
                "this request expired; the bot will ask again if still relevant"
            )
        if effective is not ApprovalStatus.PENDING:
            raise ApprovalError(f"already {effective.value}")
        return self._record(
            intent_id,
            {
                **current,
                "status": status.value,
                "decided_by": by,
                "decided_at": self._clock().isoformat(),
                "note": note,
            },
        )

    def approve(self, intent_id: str, by: str, note: str = "") -> dict:
        return self._decide(intent_id, ApprovalStatus.APPROVED, by, note)

    def reject(self, intent_id: str, by: str, note: str = "") -> dict:
        return self._decide(intent_id, ApprovalStatus.REJECTED, by, note)

    def pending(self) -> List[dict]:
        """Open requests, newest first (expired ones are left out)."""
        latest = {}
        for rec in self.journal.read("approvals"):
            latest[rec.key] = rec.data
        out = [
            d
            for d in latest.values()
            if self._effective_status(d) is ApprovalStatus.PENDING
        ]
        return sorted(out, key=lambda d: d["requested_at"], reverse=True)

    def recent(self, limit: int = 20) -> List[dict]:
        """Latest status of recent requests (for the page's history)."""
        latest = {}
        for rec in self.journal.read("approvals"):
            latest[rec.key] = rec.data
        rows = [
            {**d, "status": self._effective_status(d).value} for d in latest.values()
        ]
        return sorted(rows, key=lambda d: d["requested_at"], reverse=True)[:limit]
