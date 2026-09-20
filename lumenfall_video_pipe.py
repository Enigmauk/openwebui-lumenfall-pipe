"""Development-only Open WebUI facade for durable worker video jobs.

This module is intentionally separate from the deployed image Pipe. Its worker
origin and worker-secret path are fixed in administrator-owned code; tests
replace only the HTTP transport and secret provider with local fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


WORKER_ORIGIN = "http://lumenfall-video-worker:8000"
WORKER_BEARER_PATH = Path("/run/secrets/lumenfall-video-worker-bearer")
MAX_WORKER_RESPONSE_BYTES = 64 * 1024
DEFAULT_VIDEO_MODEL_LIST = """p-video | P-Video
wan-2.6 | Wan 2.6
seedance-2.0 | Seedance 2.0
kling-v3 | Kling V3"""
_JOB_STATES = {
    "pending_submit", "submitting", "submit_ambiguous", "queued",
    "in_progress", "poll_interrupted", "downloading", "persisting",
    "delivery_auth_required", "cancel_requested", "failed", "completed",
}
_UPSTREAM_STATES = {"queued", "in_progress", "completed", "failed"}
_SAFE_CODE = re.compile(r"^[A-Z0-9_]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class _BearerRedactor(logging.Filter):
    _TOKEN = re.compile(r"(?i)(bearer\s+)([^\s,'\"]+)")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = self._TOKEN.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


for _logger_name in ("httpx", "httpcore"):
    _logger = logging.getLogger(_logger_name)
    if not any(isinstance(item, _BearerRedactor) for item in _logger.filters):
        _logger.addFilter(_BearerRedactor())


class VideoPipeError(RuntimeError):
    """Safe, user-facing video Pipe error without provider/credential details."""


class WorkerClientError(VideoPipeError):
    """Sanitized worker transport or response error."""


class WorkerBearerProvider(Protocol):
    def get_token(self) -> str: ...


class SecretFileWorkerBearerProvider:
    """Read the separate worker credential only when a request is made."""

    def get_token(self) -> str:
        try:
            token = WORKER_BEARER_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if not token or len(token) > 4096 or any(char.isspace() for char in token):
            raise WorkerClientError("WORKER_AUTH_UNAVAILABLE")
        return token


@dataclass(frozen=True)
class WorkerJob:
    job_id: str
    state: str
    upstream_state: str | None = None
    provider: str | None = None
    executed_model: str | None = None
    cost_micros: int | None = None
    cost_currency: str | None = None
    output_mime: str | None = None
    downloaded_bytes: int | None = None
    file_id: str | None = None
    error: str | None = None
    created: bool | None = None


def _bounded_optional_text(value: Any, *, maximum: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    return value


def parse_worker_job(value: Any) -> WorkerJob:
    if not isinstance(value, dict):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    job_id = value.get("job_id")
    state = value.get("state")
    if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    if not isinstance(state, str) or state not in _JOB_STATES:
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    upstream_state = value.get("upstream_state")
    if upstream_state is not None and (
        not isinstance(upstream_state, str) or upstream_state not in _UPSTREAM_STATES
    ):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    cost = value.get("cost_micros")
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, int) or cost < 0):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    size = value.get("downloaded_bytes")
    if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    mime = value.get("output_mime")
    if mime is not None and (
        not isinstance(mime, str) or mime not in {"video/mp4", "video/webm"}
    ):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    error = _bounded_optional_text(value.get("error"), maximum=64)
    if error is not None and not _SAFE_CODE.fullmatch(error):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    created = value.get("created")
    if created is not None and not isinstance(created, bool):
        raise WorkerClientError("WORKER_RESPONSE_INVALID")
    return WorkerJob(
        job_id=job_id,
        state=state,
        upstream_state=upstream_state,
        provider=_bounded_optional_text(value.get("provider")),
        executed_model=_bounded_optional_text(value.get("executed_model")),
        cost_micros=cost,
        cost_currency=_bounded_optional_text(value.get("cost_currency"), maximum=8),
        output_mime=mime,
        downloaded_bytes=size,
        file_id=_bounded_optional_text(value.get("file_id"), maximum=128),
        error=error,
        created=created,
    )


class VideoWorkerClient:
    """Narrow authenticated client for the fixed private worker origin."""

    def __init__(self, bearer_provider: WorkerBearerProvider, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self._bearer_provider = bearer_provider
        self._client = httpx.AsyncClient(
            base_url=WORKER_ORIGIN,
            transport=transport,
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, owner_user_id: str,
                       payload: dict[str, Any] | None = None,
                       success_statuses: set[int] = {200}) -> WorkerJob:
        if not isinstance(owner_user_id, str) or not _ID.fullmatch(owner_user_id):
            raise WorkerClientError("WORKER_CONTEXT_INVALID")
        token = self._bearer_provider.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-OpenWebUI-User-Id": owner_user_id,
            "Accept": "application/json",
        }
        try:
            async with self._client.stream(method, path, headers=headers, json=payload) as response:
                if response.status_code not in success_statuses:
                    if response.status_code == 401:
                        raise WorkerClientError("WORKER_AUTH_REJECTED")
                    if response.status_code == 404:
                        raise WorkerClientError("VIDEO_JOB_NOT_FOUND")
                    if response.status_code == 409:
                        raise WorkerClientError("VIDEO_MESSAGE_CONFLICT")
                    raise WorkerClientError("WORKER_REQUEST_FAILED")
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise WorkerClientError("WORKER_RESPONSE_INVALID")
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > MAX_WORKER_RESPONSE_BYTES:
                            raise WorkerClientError("WORKER_RESPONSE_TOO_LARGE")
                    except ValueError:
                        raise WorkerClientError("WORKER_RESPONSE_INVALID") from None
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_WORKER_RESPONSE_BYTES:
                        raise WorkerClientError("WORKER_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                try:
                    parsed = json.loads(b"".join(chunks))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise WorkerClientError("WORKER_RESPONSE_INVALID") from None
                return parse_worker_job(parsed)
        except WorkerClientError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError):
            raise WorkerClientError("WORKER_UNAVAILABLE") from None

    async def create_job(self, *, owner_user_id: str, chat_id: str,
                         assistant_message_id: str, friendly_model: str,
                         model_id: str, prompt: str, options: dict[str, Any],
                         user_credential: str) -> WorkerJob:
        return await self._request(
            "POST", "/jobs", owner_user_id=owner_user_id,
            payload={
                "chat_id": chat_id,
                "assistant_message_id": assistant_message_id,
                "friendly_model": friendly_model,
                "model_id": model_id,
                "prompt": prompt,
                "options": options,
                "user_credential": user_credential,
            },
            success_statuses={202},
        )

    async def get_job(self, *, owner_user_id: str, job_id: str) -> WorkerJob:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise WorkerClientError("WORKER_CONTEXT_INVALID")
        return await self._request(
            "GET", f"/jobs/{quote(job_id, safe='')}", owner_user_id=owner_user_id
        )

    async def cancel_job(self, *, owner_user_id: str, job_id: str) -> WorkerJob:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise WorkerClientError("WORKER_CONTEXT_INVALID")
        return await self._request(
            "DELETE", f"/jobs/{quote(job_id, safe='')}", owner_user_id=owner_user_id
        )

    async def refresh_credential(self, *, owner_user_id: str, job_id: str,
                                 user_credential: str) -> WorkerJob:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise WorkerClientError("WORKER_CONTEXT_INVALID")
        return await self._request(
            "POST", f"/jobs/{quote(job_id, safe='')}/delivery-credential",
            owner_user_id=owner_user_id,
            payload={"user_credential": user_credential},
        )


def parse_video_catalog(value: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in (value or "").splitlines():
        model_id, separator, name = line.partition("|")
        model_id = model_id.strip()
        display_name = name.strip() if separator else model_id
        if (not model_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", model_id)
                or model_id in seen or not display_name or len(display_name) > 100):
            continue
        seen.add(model_id)
        entries.append((model_id, display_name))
    return entries


def current_user_id(user: Any) -> str:
    value = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise VideoPipeError("The authenticated Open WebUI user context is unavailable.")
    return value


def current_user_credential(request: Any) -> str:
    """Return exactly one current-user JWT, never the full Cookie header."""
    headers = getattr(request, "headers", None)
    cookies = getattr(request, "cookies", None)
    if headers is None or cookies is None:
        raise VideoPipeError("The authenticated Open WebUI session is unavailable.")
    authorization = headers.get("authorization", "")
    bearer = None
    if authorization:
        prefix, separator, value = authorization.partition(" ")
        if prefix.lower() != "bearer" or not separator or not value.strip():
            raise VideoPipeError("The authenticated Open WebUI session is unavailable.")
        bearer = value.strip()
    cookie_token = cookies.get("token")
    if cookie_token is not None and (not isinstance(cookie_token, str) or not cookie_token):
        raise VideoPipeError("The authenticated Open WebUI session is unavailable.")
    if bearer and cookie_token and bearer != cookie_token:
        raise VideoPipeError("The authenticated Open WebUI session is ambiguous.")
    credential = bearer or cookie_token
    if (not credential or len(credential) > 8192
            or any(char.isspace() or ord(char) < 32 for char in credential)):
        raise VideoPipeError("The authenticated Open WebUI session is unavailable.")
    return credential


def _prompt_from_body(body: Any) -> str:
    if not isinstance(body, dict):
        raise VideoPipeError("A video prompt is required.")
    messages = body.get("messages")
    prompt = None
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                prompt = message.get("content")
                break
    if prompt is None:
        prompt = body.get("prompt")
    if isinstance(prompt, list):
        text_parts = [part.get("text") for part in prompt
                      if isinstance(part, dict) and part.get("type") == "text"]
        prompt = "\n".join(part for part in text_parts if isinstance(part, str))
    if not isinstance(prompt, str):
        raise VideoPipeError("A text video prompt is required.")
    prompt = prompt.strip()
    if not prompt or len(prompt) > 20_000:
        raise VideoPipeError("A video prompt between 1 and 20000 characters is required.")
    return prompt


def _safe_options(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("video_options", {})
    if not isinstance(raw, dict) or len(raw) > 4:
        raise VideoPipeError("Video options are invalid.")
    allowed = {"seconds", "size", "aspect_ratio", "resolution"}
    if set(raw) - allowed:
        raise VideoPipeError("Video options are invalid.")
    result: dict[str, Any] = {}
    seconds = raw.get("seconds")
    if seconds is not None:
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 60:
            raise VideoPipeError("Video duration must be between 1 and 60 seconds.")
        result["seconds"] = seconds
    for field in ("size", "aspect_ratio", "resolution"):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > 64 or any(ord(c) < 32 for c in value):
                raise VideoPipeError("Video options are invalid.")
            result[field] = value
    return result


async def _maybe_await(value: Any) -> Any:
    return await value if hasattr(value, "__await__") else value


class Pipe:
    """Open WebUI video selector and bounded worker status facade."""

    class Valves(BaseModel):
        VIDEO_MODEL_LIST: str = Field(
            default=DEFAULT_VIDEO_MODEL_LIST,
            description="Development video shortlist: model-id | friendly name, editable by an administrator.",
        )
        MAX_STATUS_CHECKS: int = Field(default=2, ge=0, le=4)

    def __init__(self):
        self.valves = self.Valves()
        self._worker_client_factory: Callable[[], VideoWorkerClient] = self._default_worker_client
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def _default_worker_client(self) -> VideoWorkerClient:
        return VideoWorkerClient(SecretFileWorkerBearerProvider())

    async def pipes(self) -> list[dict[str, str]]:
        return [
            {"id": f"video:{model_id}", "name": f"LF Video · {name}"}
            for model_id, name in parse_video_catalog(self.valves.VIDEO_MODEL_LIST)
        ]

    async def _emit_status(self, emitter: Callable[..., Any], state: str,
                           job_id: str, *, done: bool = False) -> None:
        descriptions = {
            "pending_submit": "Video job queued",
            "submitting": "Video job queued",
            "submit_ambiguous": "Video submission needs attention",
            "queued": "Video job queued",
            "in_progress": "Generating video",
            "poll_interrupted": "Video generation is waiting to resume",
            "downloading": "Downloading video result",
            "persisting": "Saving video result",
            "delivery_auth_required": "Video result needs Open WebUI authentication",
            "cancel_requested": "Video cancellation requested",
            "failed": "Video job failed",
            "completed": "Video job completed",
        }
        await _maybe_await(emitter({
            "type": "status",
            "data": {"description": f"{descriptions[state]} · {job_id}", "done": done},
        }))

    async def _finish(self, job: WorkerJob,
                      emitter: Callable[..., Any]) -> str:
        await self._emit_status(emitter, job.state, job.job_id, done=job.state in {
            "completed", "failed", "submit_ambiguous", "delivery_auth_required",
        })
        if job.state == "completed":
            if not job.file_id:
                raise WorkerClientError("WORKER_RESPONSE_INVALID")
            mime = job.output_mime or "video/mp4"
            extension = "webm" if mime == "video/webm" else "mp4"
            await _maybe_await(emitter({
                "type": "files",
                "data": {"files": [{
                    "type": "file",
                    "id": job.file_id,
                    "url": f"/api/v1/files/{quote(job.file_id, safe='')}/content",
                    "name": f"lumenfall-video-{job.job_id}.{extension}",
                    "content_type": mime,
                }]},
            }))
            return "The video job completed and its saved file is available in this message."
        if job.state in {"failed", "submit_ambiguous"}:
            suffix = f" ({job.error})" if job.error else ""
            return f"Video job {job.job_id} needs attention{suffix}. Check its status before retrying."
        if job.state == "delivery_auth_required":
            return f"Video job {job.job_id} is saved; refresh Open WebUI authentication to deliver the result."
        return f"Video job {job.job_id} is {job.state}. Check this message again for status; it will not be resubmitted."

    async def pipe(
        self,
        body: dict[str, Any],
        __task__: str | None = None,
        __event_emitter__: Callable[..., Any] | None = None,
        __request__: Any = None,
        __user__: dict[str, Any] | None = None,
        __metadata__: dict[str, Any] | None = None,
        __message_id__: str | None = None,
    ) -> str:
        # Fail closed for every present and future internal task before reading
        # credentials, request metadata, client providers, or job input.
        if __task__ is not None:
            return ""

        if __event_emitter__ is None:
            raise VideoPipeError("Video jobs require an interactive Open WebUI chat.")
        if not isinstance(body, dict):
            raise VideoPipeError("Video request is invalid.")
        metadata = __metadata__ if isinstance(__metadata__, dict) else {}
        owner = current_user_id(__user__)
        action = body.get("video_action", "submit")
        client: VideoWorkerClient | None = None
        try:
            if action == "status" or action == "cancel":
                job_id = body.get("video_job_id")
                if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
                    raise VideoPipeError("A valid local video job ID is required.")
                client = self._worker_client_factory()
                if action == "cancel":
                    job = await client.cancel_job(owner_user_id=owner, job_id=job_id)
                    return await self._finish(job, __event_emitter__)
                job = await client.get_job(owner_user_id=owner, job_id=job_id)
                if job.state == "delivery_auth_required":
                    try:
                        credential = current_user_credential(__request__)
                    except VideoPipeError:
                        return await self._finish(job, __event_emitter__)
                    job = await client.refresh_credential(
                        owner_user_id=owner, job_id=job_id,
                        user_credential=credential,
                    )
                return await self._finish(job, __event_emitter__)
            if action != "submit":
                raise VideoPipeError("Video action is invalid.")

            selected = body.get("model") or ""
            if not isinstance(selected, str):
                raise VideoPipeError("Choose an LF Video model selector.")
            if not selected.startswith("video:") and "." in selected:
                selected = selected.split(".", 1)[1]
            if not selected.startswith("video:"):
                raise VideoPipeError("Choose an LF Video model selector.")
            model_id = selected[len("video:"):]
            catalog = dict(parse_video_catalog(self.valves.VIDEO_MODEL_LIST))
            if model_id not in catalog:
                raise VideoPipeError("The selected LF Video model is not available.")
            chat_id = metadata.get("chat_id")
            message_id = __message_id__ or metadata.get("message_id")
            if (not isinstance(chat_id, str) or not _ID.fullmatch(chat_id)
                    or not isinstance(message_id, str) or not _ID.fullmatch(message_id)
                    or chat_id.lower().startswith(("new-", "temp-", "unsaved-"))
                    or message_id.lower().startswith(("new-", "temp-", "unsaved-"))):
                raise VideoPipeError("Save this chat message before starting a video job.")
            prompt = _prompt_from_body(body)
            options = _safe_options(body)
            credential = current_user_credential(__request__)
            client = self._worker_client_factory()
            job = await client.create_job(
                owner_user_id=owner,
                chat_id=chat_id,
                assistant_message_id=message_id,
                friendly_model=f"LF Video · {catalog[model_id]}",
                model_id=model_id,
                prompt=prompt,
                options=options,
                user_credential=credential,
            )
            await self._emit_status(__event_emitter__, job.state, job.job_id)
            for _ in range(self.valves.MAX_STATUS_CHECKS):
                if job.state in {"completed", "failed", "submit_ambiguous", "delivery_auth_required"}:
                    break
                await self._sleep(0.1)
                job = await client.get_job(owner_user_id=owner, job_id=job.job_id)
                await self._emit_status(__event_emitter__, job.state, job.job_id)
            return await self._finish(job, __event_emitter__)
        finally:
            if client is not None:
                await client.aclose()
