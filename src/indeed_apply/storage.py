"""SQLite persistence for applications and their status history.

Two tables, created idempotently:

* ``applications``    — one row per job we have touched (unique on ``job_key``).
* ``status_history``  — an append-only log of every state transition.

The state machine is the only writer of ``status``: it calls
:meth:`Storage.record_transition`, which updates the application row *and*
appends the history row in a single transaction. Nothing else mutates
``status`` directly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import db_path as default_db_path


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Application:
    id: int
    job_key: str
    title: str
    company: str
    location: str
    url: str
    status: str
    manual_reason: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class HistoryRow:
    id: int
    application_id: int
    from_status: str | None
    to_status: str
    reason: str | None
    created_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key       TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL,
    manual_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id),
    from_status    TEXT,
    to_status      TEXT NOT NULL,
    reason         TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_app ON status_history(application_id);
"""


class Storage:
    """Thin wrapper over a single SQLite file. Cheap to construct; open per call."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()

    # -- connection -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        """Create tables + indexes if they do not exist. Idempotent."""
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # -- applications --------------------------------------------------------------

    def upsert_job(
        self,
        job_key: str,
        *,
        title: str = "",
        company: str = "",
        location: str = "",
        url: str = "",
        status: str = "PENDING",
    ) -> Application:
        """Insert a new application row, or refresh the metadata of an existing one.

        An existing row's ``status`` is never touched here — only the state
        machine changes status. Returns the current row either way.
        """
        now = _utcnow()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM applications WHERE job_key = ?", (job_key,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO applications
                        (job_key, title, company, location, url, status,
                         manual_reason, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (job_key, title, company, location, url, status, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE applications
                       SET title = ?, company = ?, location = ?, url = ?,
                           updated_at = ?
                     WHERE job_key = ?
                    """,
                    (title, company, location, url, now, job_key),
                )
        row = self.get_by_job_key(job_key)
        assert row is not None  # just wrote it
        return row

    def get(self, app_id: int) -> Application | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE id = ?", (app_id,)
            ).fetchone()
        return _to_application(row)

    def get_by_job_key(self, job_key: str) -> Application | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM applications WHERE job_key = ?", (job_key,)
            ).fetchone()
        return _to_application(row)

    def list(self, status: str | None = None) -> list[Application]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM applications ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM applications WHERE status = ? ORDER BY id",
                    (status,),
                ).fetchall()
        return [_to_application(r) for r in rows]

    def has_job(self, job_key: str) -> bool:
        """True if an application row already exists for ``job_key`` (dedup helper)."""
        return self.get_by_job_key(job_key) is not None

    # -- transitions ------------------------------------------------------------

    def record_transition(
        self,
        app_id: int,
        from_status: str | None,
        to_status: str,
        reason: str | None,
    ) -> None:
        """Apply a validated transition: update the row + append history, atomically.

        The state machine calls this *after* it has checked the transition is
        legal. ``manual_reason`` is set only when entering
        ``MANUAL_ACTION_REQUIRED`` and cleared on any other transition; the full
        reason for every transition is preserved in ``status_history``.
        """
        now = _utcnow()
        manual_reason = reason if to_status == "MANUAL_ACTION_REQUIRED" else None
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE applications
                   SET status = ?, manual_reason = ?, updated_at = ?
                 WHERE id = ?
                """,
                (to_status, manual_reason, now, app_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no application with id {app_id}")
            conn.execute(
                """
                INSERT INTO status_history
                    (application_id, from_status, to_status, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (app_id, from_status, to_status, reason, now),
            )

    def history(self, app_id: int) -> list[HistoryRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM status_history WHERE application_id = ? ORDER BY id",
                (app_id,),
            ).fetchall()
        return [
            HistoryRow(
                id=r["id"],
                application_id=r["application_id"],
                from_status=r["from_status"],
                to_status=r["to_status"],
                reason=r["reason"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


def _to_application(row: sqlite3.Row | None) -> Application | None:
    if row is None:
        return None
    return Application(
        id=row["id"],
        job_key=row["job_key"],
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"],
        status=row["status"],
        manual_reason=row["manual_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
