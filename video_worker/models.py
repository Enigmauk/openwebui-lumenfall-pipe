"""Sanitized worker states and records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class AmbiguousSubmit(ConnectionError):
    """The create request may have reached the provider; never retry it automatically."""


class WorkerState(str, Enum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTING = "submitting"
    SUBMIT_AMBIGUOUS = "submit_ambiguous"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    POLL_INTERRUPTED = "poll_interrupted"
    DOWNLOADING = "downloading"
    PERSISTING = "persisting"
    DELIVERY_AUTH_REQUIRED = "delivery_auth_required"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED = "failed"
    COMPLETED = "completed"


TERMINAL_STATES = {
    WorkerState.SUBMIT_AMBIGUOUS,
    WorkerState.FAILED,
    WorkerState.COMPLETED,
}


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    owner_user_id: str
    chat_id: str
    assistant_message_id: str
    friendly_model: str
    model_id: str
    request_fingerprint: str
    request_ciphertext: bytes | None
    idempotency_key: str
    submit_attempts: int
    upstream_job_id: str | None
    upstream_state: str | None
    worker_state: WorkerState
    poll_count: int
    next_poll_at: float | None
    last_safe_error: str | None
    provider: str | None
    executed_model: str | None
    final_cost_micros: int | None
    cost_currency: str | None
    output_mime: str | None
    expected_bytes: int | None
    downloaded_bytes: int | None
    artifact_path: str | None
    openwebui_file_id: str | None
    credential_ciphertext: bytes | None
    credential_expires_at: float | None
    cancel_requested: bool
    created_at: float
    updated_at: float
    completed_at: float | None

    def public(self) -> dict[str, Any]:
        """Return only fields safe for the authenticated Pipe to expose."""
        return {
            "job_id": self.job_id,
            "model": self.friendly_model,
            "state": self.worker_state.value,
            "upstream_state": self.upstream_state,
            "provider": self.provider,
            "executed_model": self.executed_model,
            "cost_micros": self.final_cost_micros,
            "cost_currency": self.cost_currency,
            "output_mime": self.output_mime,
            "downloaded_bytes": self.downloaded_bytes,
            "file_id": self.openwebui_file_id,
            "error": self.last_safe_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class UpstreamJob:
    job_id: str
    state: str
    provider: str | None = None
    executed_model: str | None = None
    cost_micros: int | None = None
    cost_currency: str | None = None
    output_mime: str | None = None
    output_bytes: bytes | None = None
    error_code: str | None = None
