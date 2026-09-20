"""Development-only Lumenfall video lifecycle prototype.

This module is not imported by the deployed image Function.  It models the
documented asynchronous video API and deliberately separates submission from
retryable status polling.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit


VIDEO_MODEL_LIST = """p-video | P-Video
wan-2.6 | Wan 2.6
seedance-2.0 | Seedance 2.0
kling-v3 | Kling V3"""


class VideoError(RuntimeError):
    """Sanitized video lifecycle error."""


class AmbiguousSubmission(VideoError):
    """Submission outcome is unknown; never submit again without reconciliation."""


class VideoStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class VideoOutput:
    url: str
    content_type: str
    size_bytes: int | None = None


@dataclass(frozen=True)
class VideoJob:
    job_id: str
    status: VideoStatus
    output: VideoOutput | None = None
    error_code: str | None = None


class VideoClient(Protocol):
    async def submit(self, *, model: str, prompt: str, idempotency_key: str) -> VideoJob: ...

    async def get(self, job_id: str) -> VideoJob: ...

    async def cancel(self, job_id: str) -> None: ...


def video_selector_entries(value: str = VIDEO_MODEL_LIST) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in value.splitlines():
        model_id, separator, name = line.partition("|")
        model_id, name = model_id.strip(), name.strip()
        if not model_id or model_id in seen or any(c.isspace() for c in model_id):
            continue
        seen.add(model_id)
        entries.append({"id": f"video:{model_id}", "name": f"LF Video · {name if separator else model_id}"})
    return entries


def parse_video_job(payload: Any) -> VideoJob:
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
        raise VideoError("Lumenfall returned a video response without a job ID.")
    try:
        status = VideoStatus(payload.get("status"))
    except ValueError as exc:
        raise VideoError("Lumenfall returned an unknown video status.") from exc
    output = None
    raw_output = payload.get("output")
    if status is VideoStatus.COMPLETED:
        if not isinstance(raw_output, dict) or not isinstance(raw_output.get("url"), str):
            raise VideoError("Lumenfall completed the video without an output URL.")
        content_type = raw_output.get("content_type")
        if content_type not in {"video/mp4", "video/webm"}:
            raise VideoError("Lumenfall returned an unsupported video type.")
        size = raw_output.get("size_bytes")
        if size is not None and (not isinstance(size, int) or size < 0):
            raise VideoError("Lumenfall returned an invalid video size.")
        output = VideoOutput(raw_output["url"], content_type, size)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    return VideoJob(payload["id"], status, output, error.get("code"))


def validate_video_size(output: VideoOutput, maximum_bytes: int) -> None:
    if output.size_bytes is not None and output.size_bytes > maximum_bytes:
        raise VideoError("Lumenfall returned a video larger than the configured limit.")


def validate_remote_video_url(url: str, resolved_ips: list[str] | None = None) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise VideoError("Lumenfall returned an unsafe video URL.")
    if parts.port not in (None, 443):
        raise VideoError("Lumenfall returned an unsafe video URL.")
    addresses = resolved_ips
    if addresses is None:
        try:
            addresses = list({item[4][0] for item in socket.getaddrinfo(parts.hostname, 443)})
        except OSError as exc:
            raise VideoError("The video download host could not be resolved.") from exc
    if not addresses:
        raise VideoError("The video download host could not be resolved.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise VideoError("Lumenfall returned an unsafe video URL.")


@dataclass(frozen=True)
class PollPolicy:
    initial_delay: float = 2.0
    interval: float = 5.0
    maximum_interval: float = 15.0
    maximum_wait: float = 900.0
    transient_retries: int = 4


class VideoLifecycle:
    def __init__(self, client: VideoClient, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.client = client
        self.sleep = sleep

    async def start_once(self, *, model: str, prompt: str, idempotency_key: str) -> VideoJob:
        """Submit exactly once. Transport ambiguity is never retried here."""
        try:
            return await self.client.submit(model=model, prompt=prompt, idempotency_key=idempotency_key)
        except AmbiguousSubmission:
            raise
        except (TimeoutError, ConnectionError) as exc:
            raise AmbiguousSubmission(
                "Video submission outcome is unknown; reconcile by idempotency key before any retry."
            ) from exc

    async def poll(
        self,
        job_id: str,
        policy: PollPolicy = PollPolicy(),
        progress: Callable[[VideoStatus], Awaitable[None]] | None = None,
    ) -> VideoJob:
        elapsed, delay, failures = 0.0, policy.initial_delay, 0
        if delay:
            await self.sleep(delay)
            elapsed += delay
        while elapsed <= policy.maximum_wait:
            try:
                job = await self.client.get(job_id)
                failures = 0
            except (TimeoutError, ConnectionError):
                failures += 1
                if failures > policy.transient_retries:
                    raise VideoError(f"Polling was interrupted; the known video job is {job_id}.")
                job = None
            if job is not None:
                if progress:
                    await progress(job.status)
                if job.status is VideoStatus.COMPLETED:
                    return job
                if job.status is VideoStatus.FAILED:
                    raise VideoError(f"Lumenfall video job {job_id} failed ({job.error_code or 'unknown'}).")
            await self.sleep(delay)
            elapsed += delay
            delay = min(policy.maximum_interval, max(policy.interval, delay * 1.5))
        raise VideoError(f"Polling timed out; the known video job is {job_id}. Do not submit again.")


class VideoPrototypePipe:
    """Minimal Stage-1 boundary proving internal tasks cannot submit jobs."""

    def __init__(self, lifecycle: VideoLifecycle):
        self.lifecycle = lifecycle

    async def pipe(self, *, task: str | None, model: str, prompt: str, idempotency_key: str):
        if task is not None:
            return ""
        return await self.lifecycle.start_once(
            model=model, prompt=prompt, idempotency_key=idempotency_key
        )
