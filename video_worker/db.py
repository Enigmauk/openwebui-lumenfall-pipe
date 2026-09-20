"""SQLite job store with durable, checked state transitions."""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import JobRecord, WorkerState


class JobNotFound(LookupError):
    pass


class DuplicateRequestConflict(ValueError):
    pass


class InvalidTransition(ValueError):
    pass


TRANSITIONS: dict[WorkerState, set[WorkerState]] = {
    WorkerState.PENDING_SUBMIT: {
        WorkerState.SUBMITTING, WorkerState.CANCEL_REQUESTED, WorkerState.FAILED,
    },
    WorkerState.SUBMITTING: {
        WorkerState.QUEUED, WorkerState.IN_PROGRESS, WorkerState.SUBMIT_AMBIGUOUS,
        WorkerState.FAILED,
    },
    WorkerState.QUEUED: {
        WorkerState.QUEUED, WorkerState.IN_PROGRESS, WorkerState.DOWNLOADING,
        WorkerState.POLL_INTERRUPTED, WorkerState.CANCEL_REQUESTED, WorkerState.FAILED,
    },
    WorkerState.IN_PROGRESS: {
        WorkerState.QUEUED, WorkerState.IN_PROGRESS, WorkerState.DOWNLOADING,
        WorkerState.POLL_INTERRUPTED, WorkerState.CANCEL_REQUESTED, WorkerState.FAILED,
    },
    WorkerState.POLL_INTERRUPTED: {
        WorkerState.QUEUED, WorkerState.IN_PROGRESS, WorkerState.DOWNLOADING,
        WorkerState.POLL_INTERRUPTED, WorkerState.CANCEL_REQUESTED, WorkerState.FAILED,
    },
    WorkerState.CANCEL_REQUESTED: {
        WorkerState.QUEUED, WorkerState.IN_PROGRESS, WorkerState.DOWNLOADING,
        WorkerState.POLL_INTERRUPTED, WorkerState.CANCEL_REQUESTED, WorkerState.FAILED,
    },
    WorkerState.DOWNLOADING: {
        WorkerState.PERSISTING, WorkerState.DELIVERY_AUTH_REQUIRED, WorkerState.FAILED,
    },
    WorkerState.PERSISTING: {
        WorkerState.PERSISTING, WorkerState.DELIVERY_AUTH_REQUIRED,
        WorkerState.COMPLETED, WorkerState.FAILED,
    },
    WorkerState.DELIVERY_AUTH_REQUIRED: {
        WorkerState.PERSISTING, WorkerState.DELIVERY_AUTH_REQUIRED, WorkerState.FAILED,
    },
    WorkerState.SUBMIT_AMBIGUOUS: set(),
    WorkerState.FAILED: set(),
    WorkerState.COMPLETED: set(),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
 assistant_message_id TEXT NOT NULL, friendly_model TEXT NOT NULL,
 model_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
 request_ciphertext BLOB, idempotency_key TEXT NOT NULL UNIQUE,
 submit_attempts INTEGER NOT NULL DEFAULT 0, upstream_job_id TEXT,
 upstream_state TEXT, worker_state TEXT NOT NULL, poll_count INTEGER NOT NULL DEFAULT 0,
 next_poll_at REAL, last_safe_error TEXT, provider TEXT, executed_model TEXT,
 final_cost_micros INTEGER, cost_currency TEXT, output_mime TEXT,
 expected_bytes INTEGER, downloaded_bytes INTEGER, artifact_path TEXT,
 openwebui_file_id TEXT, credential_ciphertext BLOB, credential_expires_at REAL,
 cancel_requested INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, completed_at REAL,
 UNIQUE(owner_user_id, chat_id, assistant_message_id)
);
CREATE INDEX IF NOT EXISTS jobs_state_poll ON jobs(worker_state, next_poll_at);
"""


class JobStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA synchronous=FULL")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > self.SCHEMA_VERSION:
            raise RuntimeError("Worker database schema is newer than this code.")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except Exception:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    @staticmethod
    def record(row: sqlite3.Row | None) -> JobRecord | None:
        if row is None:
            return None
        values = dict(row)
        values["worker_state"] = WorkerState(values["worker_state"])
        values["cancel_requested"] = bool(values["cancel_requested"])
        return JobRecord(**values)

    def create_or_get(self, **values) -> tuple[JobRecord, bool]:
        now = time.time()
        with self.transaction() as db:
            existing = db.execute(
                "SELECT * FROM jobs WHERE owner_user_id=? AND chat_id=? AND assistant_message_id=?",
                (values["owner_user_id"], values["chat_id"], values["assistant_message_id"]),
            ).fetchone()
            if existing:
                if existing["request_fingerprint"] != values["request_fingerprint"]:
                    raise DuplicateRequestConflict("Message already has a different video job.")
                return self.record(existing), False
            job_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO jobs
                (job_id, owner_user_id, chat_id, assistant_message_id, friendly_model,
                 model_id, request_fingerprint, request_ciphertext, idempotency_key,
                 credential_ciphertext, credential_expires_at, worker_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, values["owner_user_id"], values["chat_id"],
                 values["assistant_message_id"], values["friendly_model"],
                 values["model_id"], values["request_fingerprint"],
                 values["request_ciphertext"], values["idempotency_key"],
                 values["credential_ciphertext"], values["credential_expires_at"],
                 WorkerState.PENDING_SUBMIT.value, now, now),
            )
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self.record(row), True

    def get(self, job_id: str, owner_user_id: str | None = None) -> JobRecord | None:
        sql, params = ("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if owner_user_id is not None:
            sql += " AND owner_user_id=?"
            params += (owner_user_id,)
        with self._lock:
            return self.record(self._db.execute(sql, params).fetchone())

    def claim_submission(self, job_id: str) -> JobRecord | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["worker_state"] != WorkerState.PENDING_SUBMIT.value:
                return None
            if row["submit_attempts"]:
                raise InvalidTransition("A job cannot be submitted twice.")
            db.execute(
                "UPDATE jobs SET worker_state=?, submit_attempts=1, updated_at=? WHERE job_id=?",
                (WorkerState.SUBMITTING.value, time.time(), job_id),
            )
            return self.record(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def transition(self, job_id: str, state: WorkerState, **fields) -> JobRecord:
        allowed = set(JobRecord.__dataclass_fields__) - {
            "job_id", "owner_user_id", "chat_id", "assistant_message_id",
            "friendly_model", "model_id", "request_fingerprint", "idempotency_key",
            "submit_attempts", "worker_state", "created_at", "updated_at",
        }
        if not set(fields) <= allowed:
            raise ValueError("Unsupported worker state field.")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise JobNotFound(job_id)
            current = WorkerState(row["worker_state"])
            if state != current and state not in TRANSITIONS[current]:
                raise InvalidTransition(f"Cannot move {current.value} to {state.value}.")
            updates = {**fields, "worker_state": state.value, "updated_at": time.time()}
            if "cancel_requested" in updates:
                updates["cancel_requested"] = int(bool(updates["cancel_requested"]))
            assignments = ", ".join(f"{name}=?" for name in updates)
            db.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?", (*updates.values(), job_id))
            return self.record(db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def recover(self) -> list[JobRecord]:
        """Fail closed on unknown submits, then return safely resumable jobs."""
        with self.transaction() as db:
            db.execute(
                """UPDATE jobs SET worker_state=?, request_ciphertext=NULL,
                   credential_ciphertext=NULL, credential_expires_at=NULL,
                   last_safe_error=?, updated_at=?
                   WHERE worker_state=? AND upstream_job_id IS NULL""",
                (WorkerState.SUBMIT_AMBIGUOUS.value, "SUBMIT_OUTCOME_UNKNOWN", time.time(),
                 WorkerState.SUBMITTING.value),
            )
            placeholders = ",".join("?" for _ in range(7))
            rows = db.execute(
                f"SELECT * FROM jobs WHERE worker_state IN ({placeholders}) ORDER BY created_at",
                tuple(state.value for state in (
                    WorkerState.PENDING_SUBMIT, WorkerState.QUEUED,
                    WorkerState.IN_PROGRESS, WorkerState.POLL_INTERRUPTED,
                    WorkerState.DOWNLOADING, WorkerState.PERSISTING,
                    WorkerState.CANCEL_REQUESTED,
                )),
            ).fetchall()
        return [self.record(row) for row in rows]

    def ping(self) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1").fetchone()[0] == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()
