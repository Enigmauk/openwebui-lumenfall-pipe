"""Fixed-origin Lumenfall video API collaborator with no POST retries."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from .models import AmbiguousSubmit, PermanentVideoError, UpstreamJob


class LumenfallKeyProvider(Protocol):
    def get_key(self) -> str: ...


class LumenfallVideoClient:
    """HTTP implementation of ``VideoBackend`` using the fixed public origin.

    A transport can be injected for tests. No base URL option is exposed, and
    this class never reads a key from the environment or a file itself.
    """

    ORIGIN = "https://api.lumenfall.ai"
    VIDEOS_PATH = "/openai/v1/videos"
    MAX_RESPONSE_BYTES = 512 * 1024
    GET_ATTEMPTS = 3
    _STATES = {"queued", "in_progress", "completed", "failed"}

    def __init__(self, key_provider: LumenfallKeyProvider, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep):
        self._key_provider = key_provider
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=self.ORIGIN,
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    def _headers(self) -> dict[str, str]:
        try:
            key = self._key_provider.get_key()
        except Exception:
            raise PermanentVideoError("LUMENFALL_KEY_UNAVAILABLE") from None
        if not isinstance(key, str) or not key or "\r" in key or "\n" in key:
            raise PermanentVideoError("LUMENFALL_KEY_UNAVAILABLE")
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def _read_json(self, response: httpx.Response) -> dict:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) < 0 or int(declared) > self.MAX_RESPONSE_BYTES:
                    raise PermanentVideoError("UPSTREAM_RESPONSE_TOO_LARGE")
            except ValueError:
                raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE") from None
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
            if len(body) + len(chunk) > self.MAX_RESPONSE_BYTES:
                raise PermanentVideoError("UPSTREAM_RESPONSE_TOO_LARGE")
            body.extend(chunk)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE") from None
        if not isinstance(value, dict):
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE")
        return value

    async def submit(self, *, model_id: str, prompt: str, options: dict,
                     idempotency_key: str) -> UpstreamJob:
        if not isinstance(options, dict) or set(options) & {"model", "prompt", "idempotency_key"}:
            raise PermanentVideoError("INVALID_VIDEO_REQUEST")
        body = {"model": model_id, "prompt": prompt, **options,
                "idempotency_key": idempotency_key}
        # There is deliberately one stream/request invocation here. In
        # particular, no transport retry wrapper is installed around POST.
        try:
            async with self._client.stream(
                "POST", self.VIDEOS_PATH, json=body, headers=self._headers()
            ) as response:
                if response.status_code != 202:
                    if (response.status_code >= 500 or response.status_code in {408, 425}
                            or 200 <= response.status_code < 300):
                        raise AmbiguousSubmit("LUMENFALL_CREATE_OUTCOME_UNKNOWN") from None
                    raise PermanentVideoError("LUMENFALL_CREATE_REJECTED")
                try:
                    payload = await self._read_json(response)
                    job = self._parse_job(payload)
                except PermanentVideoError:
                    # A 202 may represent a paid job even if its response is
                    # unusable. Keep this outcome ambiguous and never resubmit.
                    raise AmbiguousSubmit("LUMENFALL_CREATE_OUTCOME_UNKNOWN") from None
                if job.state not in {"queued", "in_progress"}:
                    raise AmbiguousSubmit("LUMENFALL_CREATE_OUTCOME_UNKNOWN") from None
                return job
        except (AmbiguousSubmit, PermanentVideoError):
            raise
        except httpx.TransportError:
            raise AmbiguousSubmit("LUMENFALL_CREATE_OUTCOME_UNKNOWN") from None

    async def get(self, upstream_id: str) -> UpstreamJob:
        path = self._job_path(upstream_id)
        for attempt in range(self.GET_ATTEMPTS):
            try:
                async with self._client.stream("GET", path, headers=self._headers()) as response:
                    if response.status_code in {408, 425, 429} or response.status_code >= 500:
                        if attempt + 1 < self.GET_ATTEMPTS:
                            await self._sleep(0.1 * (2 ** attempt))
                            continue
                        raise ConnectionError("LUMENFALL_POLL_TEMPORARY_FAILURE") from None
                    if response.status_code != 200:
                        raise PermanentVideoError("LUMENFALL_POLL_REJECTED")
                    payload = await self._read_json(response)
                    job = self._parse_job(payload, expected_id=upstream_id)
                    return job
            except httpx.TransportError:
                if attempt + 1 < self.GET_ATTEMPTS:
                    await self._sleep(0.1 * (2 ** attempt))
                    continue
                raise ConnectionError("LUMENFALL_POLL_TEMPORARY_FAILURE") from None
        raise ConnectionError("LUMENFALL_POLL_TEMPORARY_FAILURE")

    async def cancel(self, upstream_id: str) -> None:
        try:
            async with self._client.stream(
                "DELETE", self._job_path(upstream_id), headers=self._headers()
            ) as response:
                if 200 <= response.status_code < 300 or response.status_code == 404:
                    return
                raise PermanentVideoError("LUMENFALL_CANCEL_FAILED")
        except httpx.TransportError:
            raise ConnectionError("LUMENFALL_CANCEL_FAILED") from None

    @classmethod
    def _job_path(cls, upstream_id: str) -> str:
        if not isinstance(upstream_id, str) or not upstream_id or len(upstream_id) > 256:
            raise PermanentVideoError("INVALID_UPSTREAM_ID")
        if any(ord(character) < 32 for character in upstream_id):
            raise PermanentVideoError("INVALID_UPSTREAM_ID")
        return f"{cls.VIDEOS_PATH}/{quote(upstream_id, safe='')}"

    @classmethod
    def _parse_job(cls, payload: dict, *, expected_id: str | None = None) -> UpstreamJob:
        upstream_id = payload.get("id")
        if (not isinstance(upstream_id, str) or not upstream_id or len(upstream_id) > 256
                or any(ord(character) < 32 for character in upstream_id)):
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE")
        if expected_id is not None and upstream_id != expected_id:
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE")
        state = payload.get("status")
        if state not in cls._STATES:
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE")

        metadata = payload.get("metadata")
        if metadata is None:
            metadata = {}
        if not isinstance(metadata, dict):
            raise PermanentVideoError("INVALID_UPSTREAM_RESPONSE")
        provider = cls._optional_text(metadata.get("provider"), "provider")
        executed_model = cls._optional_text(metadata.get("executed_model"), "executed_model")

        cost_micros = None
        currency = metadata.get("cost_currency")
        if currency is not None and (not isinstance(currency, str)
                                     or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise PermanentVideoError("INVALID_RESULT_METADATA")
        if state == "completed" and metadata.get("cost") is not None:
            raw_cost = metadata["cost"]
            if isinstance(raw_cost, bool):
                raise PermanentVideoError("INVALID_RESULT_METADATA")
            try:
                amount = Decimal(str(raw_cost))
                if not amount.is_finite() or amount < 0 or currency is None:
                    raise InvalidOperation
                cost_micros = int((amount * Decimal(1_000_000)).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                ))
            except (InvalidOperation, ValueError, TypeError, OverflowError):
                raise PermanentVideoError("INVALID_RESULT_METADATA") from None

        output_url = None
        output_mime = None
        expected_bytes = None
        if state == "completed":
            output = payload.get("output")
            if not isinstance(output, dict):
                raise PermanentVideoError("INVALID_RESULT_METADATA")
            output_url = output.get("url")
            if not isinstance(output_url, str) or len(output_url) > 8192:
                raise PermanentVideoError("INVALID_RESULT_METADATA")
            parsed = urlsplit(output_url)
            if (parsed.scheme.lower() != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise PermanentVideoError("INVALID_RESULT_METADATA")
            raw_mime = output.get("content_type")
            output_mime = raw_mime.split(";", 1)[0].strip().lower() if isinstance(raw_mime, str) else None
            if output_mime not in {"video/mp4", "video/webm"}:
                raise PermanentVideoError("INVALID_RESULT_METADATA")
            raw_size = output.get("size_bytes")
            if raw_size is not None:
                if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size <= 0:
                    raise PermanentVideoError("INVALID_RESULT_METADATA")
                expected_bytes = raw_size

        error_code = None
        if state == "failed":
            error = payload.get("error")
            raw_code = error.get("code") if isinstance(error, dict) else None
            error_code = (raw_code if isinstance(raw_code, str)
                          and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw_code)
                          else "UPSTREAM_FAILED")

        return UpstreamJob(
            job_id=upstream_id,
            state=state,
            provider=provider,
            executed_model=executed_model,
            cost_micros=cost_micros,
            cost_currency=currency,
            output_mime=output_mime,
            error_code=error_code,
            output_url=output_url,
            expected_bytes=expected_bytes,
        )

    @staticmethod
    def _optional_text(value, field: str) -> str | None:
        if value is None:
            return None
        if (not isinstance(value, str) or not value or len(value) > 256
                or any(ord(character) < 32 for character in value)):
            raise PermanentVideoError("INVALID_RESULT_METADATA")
        return value

    async def aclose(self) -> None:
        await self._client.aclose()
