"""Append-only SQLite journal (plan T04, spec §9.1).

Every decision, risk check, order event, fill and snapshot is written here
and never changed. SQLite triggers reject UPDATE and DELETE, so the journal
can't be rewritten by a bug. Current state (e.g. the active halt) is derived
from the latest record.

Each table has the same shape:

    id      INTEGER PRIMARY KEY   insertion order
    ts      TEXT                  ISO-8601 UTC timestamp of the event
    key     TEXT                  lookup key (intent ID, order ref, chain ID, ...)
    payload TEXT                  JSON from domain.to_dict()
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

from src.trading.domain import from_dict, to_dict, utcnow

DEFAULT_JOURNAL_PATH = Path("data/trading/journal.db")

TABLES: Sequence[str] = (
    "intents",
    "risk_checks",
    "orders",
    "order_events",
    "fills",
    "chains",
    "positions_snapshot",
    "account_snapshot",
    "budget_snapshot",
    "scan_results",
    "halts",
    "config_versions",
    "reconciliations",
)


def _create_table(table: str) -> List[str]:
    return [
        f"""CREATE TABLE IF NOT EXISTS {table} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            key TEXT,
            payload TEXT NOT NULL
        )""",
        f"CREATE INDEX IF NOT EXISTS idx_{table}_key ON {table}(key)",
        f"CREATE INDEX IF NOT EXISTS idx_{table}_ts ON {table}(ts)",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END""",
    ]


# Each migration is a list of SQL statements. Never edit a released migration;
# append a new one instead.
_V1_TABLES = TABLES[: TABLES.index("config_versions") + 1]

MIGRATIONS: List[List[str]] = [
    # v1: the spec §9.1 tables
    [stmt for table in _V1_TABLES for stmt in _create_table(table)],
    # v2 (T13): reconciliation results
    _create_table("reconciliations"),
]


class JournalError(Exception):
    pass


class Record:
    """A journal row with its payload decoded back into domain objects."""

    __slots__ = ("id", "table", "ts", "key", "data")

    def __init__(self, id_: int, table: str, ts: str, key: Optional[str], payload: str):
        self.id = id_
        self.table = table
        self.ts = datetime.fromisoformat(ts)
        self.key = key
        self.data = from_dict(json.loads(payload))

    def __repr__(self) -> str:
        return (
            f"Record({self.table}#{self.id} key={self.key!r} ts={self.ts.isoformat()})"
        )


class Journal:
    """Thread-safe append-only event store."""

    def __init__(self, path: Path | str = DEFAULT_JOURNAL_PATH):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # -- schema ---------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT MAX(version) FROM schema_version"
            ).fetchone()
            current = row[0] or 0
            if current > len(MIGRATIONS):
                raise JournalError(
                    f"journal schema v{current} is newer than this code "
                    f"(v{len(MIGRATIONS)}); refusing to run"
                )
            for version in range(current + 1, len(MIGRATIONS) + 1):
                for stmt in MIGRATIONS[version - 1]:
                    self._conn.execute(stmt)
                self._conn.execute("INSERT INTO schema_version VALUES (?)", (version,))

    @property
    def schema_version(self) -> int:
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return row[0] or 0

    # -- write ----------------------------------------------------------------

    def append(
        self,
        table: str,
        obj: Any,
        key: Optional[str] = None,
        ts: Optional[datetime] = None,
    ) -> int:
        """Append one record and return its row ID."""
        self._check_table(table)
        ts = ts or utcnow()
        payload = json.dumps(to_dict(obj), separators=(",", ":"))
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO {table} (ts, key, payload) VALUES (?, ?, ?)",
                (ts.isoformat(), key, payload),
            )
            return cur.lastrowid

    # -- read -----------------------------------------------------------------

    def read(
        self,
        table: str,
        key: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> List[Record]:
        """Records in insertion order, optionally filtered by key and time."""
        return list(self._iter(table, key, since, until, limit, newest_first=False))

    def latest(self, table: str, key: Optional[str] = None) -> Optional[Record]:
        records = list(self._iter(table, key, None, None, 1, newest_first=True))
        return records[0] if records else None

    def count(self, table: str) -> int:
        self._check_table(table)
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def _iter(self, table, key, since, until, limit, newest_first) -> Iterator[Record]:
        self._check_table(table)
        sql = f"SELECT id, ts, key, payload FROM {table}"
        clauses, params = [], []
        if key is not None:
            clauses.append("key = ?")
            params.append(key)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since.isoformat())
        if until is not None:
            clauses.append("ts < ?")
            params.append(until.isoformat())
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC" if newest_first else " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        for id_, ts, k, payload in rows:
            yield Record(id_, table, ts, k, payload)

    # -- misc -----------------------------------------------------------------

    @staticmethod
    def _check_table(table: str) -> None:
        # Table names are interpolated into SQL, so only known names are allowed.
        if table not in TABLES:
            raise JournalError(f"unknown journal table: {table!r}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
