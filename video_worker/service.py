"""Durable worker lifecycle, driven one safe transition at a time."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Protocol

from .db import JobStore
from .models import (
    AmbiguousSubmit, JobRecord, PermanentVideoError, TERMINAL_STATES,
    UpstreamJob, WorkerState,
)
from .security import SecretBox, canonical_request, new_idempotency_key, request_fingerprint


class VideoBackend(Protocol):
    async def submit(self, *, model_id: str, prompt: str, options: dict,
                     idempotency_key: str) -> UpstreamJob: ...
    async def get(self, upstream_id: str) -> UpstreamJob: ...
    async def cancel(self, upstream_id: str) -> None: ...


class Persistence(Protocol):
    async def persist(self, *, job_id: str, content: bytes, mime: str,
                      credential: str) -> str: ...


class ResultDownloader(Protocol):
    async def download(self, *, job_id: str, url: str, mime: str,
                       expected_bytes: int | None = None): ...


class WorkerService:
    CREDENTIAL_TTL = 6 * 60 * 60

    def __init__(self, store: JobStore, backend: VideoBackend, persistence: Persistence,
                 secret_box: SecretBox, fingerprint_key: bytes,
                 artifact_directory: str | Path | None = None,
                 downloader: ResultDownloader | None = None):
        self.store = store
        self.backend = backend
        self.persistence = persistence
        self.secret_box = secret_box
        self.fingerprint_key = fingerprint_key
        default_directory = Path(store.path).parent / "artifacts"
        self.artifact_directory = Path(artifact_directory or default_directory)
        self.artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.downloader = downloader

    @staticmethod
    def context(owner: str, chat: str, message: str) -> str:
        return f"video-job:{owner}:{chat}:{message}"

    def create_job(self, *, owner_user_id: str, chat_id: str,
                   assistant_message_id: str, friendly_model: str, model_id: str,
                   prompt: str, options: dict, user_credential: str) -> tuple[JobRecord, bool]:
        request = canonical_request(model_id, prompt, options)
        context = self.context(owner_user_id, chat_id, assistant_message_id)
        return self.store.create_or_get(
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            assistant_message_id=assistant_message_id,
            friendly_model=friendly_model,
            model_id=model_id,
            request_fingerprint=request_fingerprint(self.fingerprint_key, request),
            request_ciphertext=self.secret_box.encrypt(request, context=context),
            idempotency_key=new_idempotency_key(),
            credential_ciphertext=self.secret_box.encrypt(user_credential.encode(), context=context),
            credential_expires_at=time.time() + self.CREDENTIAL_TTL,
        )

    def owned(self, job_id: str, owner_user_id: str) -> JobRecord | None:
        return self.store.get(job_id, owner_user_id)

    async def advance(self, job_id: str) -> JobRecord:
        job = self.store.get(job_id)
        if not job:
            raise LookupError(job_id)
        if job.worker_state in TERMINAL_STATES:
            return job
        if job.worker_state is WorkerState.PENDING_SUBMIT:
            return await self._submit(job_id)
        if job.worker_state in {
            WorkerState.QUEUED, WorkerState.IN_PROGRESS,
            WorkerState.POLL_INTERRUPTED, WorkerState.CANCEL_REQUESTED,
        }:
            return await self._poll(job)
        if job.worker_state is WorkerState.DOWNLOADING:
            return self.store.transition(job_id, WorkerState.PERSISTING)
        if job.worker_state is WorkerState.PERSISTING:
            return await self._persist(job)
        return job

    async def _submit(self, job_id: str) -> JobRecord:
        job = self.store.claim_submission(job_id)
        if not job:
            return self.store.get(job_id)
        context = self.context(job.owner_user_id, job.chat_id, job.assistant_message_id)
        request = json.loads(self.secret_box.decrypt(job.request_ciphertext, context=context))
        try:
            upstream = await self.backend.submit(
                model_id=request["model_id"], prompt=request["prompt"],
                options=request["options"], idempotency_key=job.idempotency_key,
            )
        except (AmbiguousSubmit, TimeoutError, ConnectionError):
            return self.store.transition(
                job_id, WorkerState.SUBMIT_AMBIGUOUS, request_ciphertext=None,
                credential_ciphertext=None, credential_expires_at=None,
                last_safe_error="SUBMIT_OUTCOME_UNKNOWN",
            )
        except Exception:
            return self.store.transition(
                job_id, WorkerState.FAILED, request_ciphertext=None,
                credential_ciphertext=None, credential_expires_at=None,
                last_safe_error="SUBMIT_REJECTED",
            )
        if (not isinstance(upstream, UpstreamJob) or not upstream.job_id
                or upstream.state not in {"queued", "in_progress"}):
            return self.store.transition(
                job_id, WorkerState.SUBMIT_AMBIGUOUS, request_ciphertext=None,
                credential_ciphertext=None, credential_expires_at=None,
                last_safe_error="SUBMIT_OUTCOME_UNKNOWN",
            )
        state = WorkerState.QUEUED if upstream.state == "queued" else WorkerState.IN_PROGRESS
        return self.store.transition(
            job_id, state, upstream_job_id=upstream.job_id,
            upstream_state=upstream.state, request_ciphertext=None,
        )

    async def _poll(self, job: JobRecord) -> JobRecord:
        if job.cancel_requested and job.upstream_job_id:
            try:
                await self.backend.cancel(job.upstream_job_id)
            except Exception:
                pass
        try:
            upstream = await self.backend.get(job.upstream_job_id)
        except (TimeoutError, ConnectionError):
            return self.store.transition(
                job.job_id, WorkerState.POLL_INTERRUPTED,
                poll_count=job.poll_count + 1, last_safe_error="POLL_TRANSIENT",
            )
        except PermanentVideoError:
            return self.store.transition(
                job.job_id, WorkerState.FAILED, poll_count=job.poll_count + 1,
                last_safe_error="INVALID_UPSTREAM_RESPONSE",
                credential_ciphertext=None, credential_expires_at=None,
            )
        if (not isinstance(upstream, UpstreamJob) or upstream.job_id != job.upstream_job_id
                or upstream.state not in {"queued", "in_progress", "completed", "failed"}):
            return self.store.transition(
                job.job_id, WorkerState.FAILED, poll_count=job.poll_count + 1,
                last_safe_error="INVALID_UPSTREAM_RESPONSE",
                credential_ciphertext=None, credential_expires_at=None,
            )
        common = {
            "upstream_state": upstream.state,
            "poll_count": job.poll_count + 1,
            "provider": upstream.provider,
            "executed_model": upstream.executed_model,
        }
        if upstream.state == "failed":
            return self.store.transition(
                job.job_id, WorkerState.FAILED, **common,
                last_safe_error=upstream.error_code or "UPSTREAM_FAILED",
                credential_ciphertext=None, credential_expires_at=None,
            )
        if upstream.state == "completed":
            if upstream.output_url and self.downloader:
                if upstream.output_mime not in {"video/mp4", "video/webm"}:
                    return self.store.transition(
                        job.job_id, WorkerState.FAILED, **common,
                        last_safe_error="INVALID_OUTPUT", credential_ciphertext=None,
                        credential_expires_at=None,
                    )
                try:
                    artifact = await self.downloader.download(
                        job_id=job.job_id, url=upstream.output_url,
                        mime=upstream.output_mime,
                        expected_bytes=upstream.expected_bytes,
                    )
                except PermanentVideoError:
                    return self.store.transition(
                        job.job_id, WorkerState.FAILED, **common,
                        last_safe_error="RESULT_DOWNLOAD_REJECTED",
                        credential_ciphertext=None, credential_expires_at=None,
                    )
                return self.store.transition(
                    job.job_id, WorkerState.DOWNLOADING, **common,
                    last_safe_error=None,
                    final_cost_micros=upstream.cost_micros,
                    cost_currency=upstream.cost_currency,
                    output_mime=upstream.output_mime,
                    expected_bytes=upstream.expected_bytes,
                    downloaded_bytes=artifact.actual_bytes,
                    artifact_path=artifact.artifact_path,
                )
            if not upstream.output_bytes or upstream.output_mime not in {"video/mp4", "video/webm"}:
                return self.store.transition(
                    job.job_id, WorkerState.FAILED, **common,
                    last_safe_error="INVALID_OUTPUT", credential_ciphertext=None,
                    credential_expires_at=None,
                )
            temporary = self.artifact_directory / f".{job.job_id}.part"
            final = self.artifact_directory / f"{job.job_id}.video"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(upstream.output_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(final)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            return self.store.transition(
                job.job_id, WorkerState.DOWNLOADING, **common,
                last_safe_error=None,
                final_cost_micros=upstream.cost_micros,
                cost_currency=upstream.cost_currency,
                output_mime=upstream.output_mime,
                expected_bytes=len(upstream.output_bytes),
                downloaded_bytes=len(upstream.output_bytes),
                artifact_path=final.name,
            )
        state = WorkerState.QUEUED if upstream.state == "queued" else WorkerState.IN_PROGRESS
        return self.store.transition(job.job_id, state, **common, last_safe_error=None)

    async def _persist(self, job: JobRecord) -> JobRecord:
        context = self.context(job.owner_user_id, job.chat_id, job.assistant_message_id)
        if not job.credential_ciphertext or not job.credential_expires_at or job.credential_expires_at <= time.time():
            return self.store.transition(
                job.job_id, WorkerState.DELIVERY_AUTH_REQUIRED,
                credential_ciphertext=None, credential_expires_at=None,
                last_safe_error="DELIVERY_AUTH_REQUIRED",
            )
        credential = self.secret_box.decrypt(job.credential_ciphertext, context=context).decode()
        try:
            allowed_artifacts = {
                f"{job.job_id}.video", f"{job.job_id}.mp4", f"{job.job_id}.webm",
            }
            if not job.artifact_path or job.artifact_path not in allowed_artifacts:
                raise OSError("Invalid protected artifact location.")
            artifact = self.artifact_directory / job.artifact_path
            if artifact.is_symlink() or not artifact.is_file():
                raise OSError("Invalid protected artifact location.")
            file_id = await self.persistence.persist(
                job_id=job.job_id, content=artifact.read_bytes(),
                mime=job.output_mime, credential=credential,
            )
        except PermissionError:
            return self.store.transition(
                job.job_id, WorkerState.DELIVERY_AUTH_REQUIRED,
                credential_ciphertext=None, credential_expires_at=None,
                last_safe_error="DELIVERY_AUTH_REQUIRED",
            )
        artifact.unlink(missing_ok=True)
        return self.store.transition(
            job.job_id, WorkerState.COMPLETED, openwebui_file_id=file_id,
            artifact_path=None, credential_ciphertext=None,
            credential_expires_at=None, completed_at=time.time(),
        )

    async def run_until_stable(self, job_id: str, maximum_steps: int = 20) -> JobRecord:
        job = self.store.get(job_id)
        for _ in range(maximum_steps):
            if job.worker_state in TERMINAL_STATES | {WorkerState.DELIVERY_AUTH_REQUIRED}:
                return job
            job = await self.advance(job_id)
        return job

    async def request_cancel(self, job_id: str, owner_user_id: str) -> JobRecord | None:
        job = self.store.get(job_id, owner_user_id)
        if not job:
            return None
        if job.worker_state in TERMINAL_STATES:
            return job
        return self.store.transition(
            job_id, WorkerState.CANCEL_REQUESTED, cancel_requested=True
        )

    def refresh_credential(self, job_id: str, owner_user_id: str,
                           credential: str) -> JobRecord | None:
        job = self.store.get(job_id, owner_user_id)
        if not job:
            return None
        context = self.context(job.owner_user_id, job.chat_id, job.assistant_message_id)
        state = (WorkerState.PERSISTING if job.worker_state is WorkerState.DELIVERY_AUTH_REQUIRED
                 else job.worker_state)
        return self.store.transition(
            job_id, state,
            credential_ciphertext=self.secret_box.encrypt(credential.encode(), context=context),
            credential_expires_at=time.time() + self.CREDENTIAL_TTL,
            last_safe_error=None,
        )
