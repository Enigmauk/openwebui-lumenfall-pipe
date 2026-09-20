"""
title: Lumenfall Media Pipe
author: Enigmauk
version: 0.1.0
required_open_webui_version: 0.11.3
description: Curated Lumenfall image models with user-owned Open WebUI file persistence.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, Field


LUMENFALL_BASE_URL = "https://api.lumenfall.ai/openai/v1"
IMAGE_GENERATIONS_URL = f"{LUMENFALL_BASE_URL}/images/generations"
LUMENFALL_SECRET_PATH = Path("/run/secrets/lumenfall-api-key")
INTERNAL_TASK_RESPONSE = ""
DEFAULT_MODEL_LIST = """seedream-5-lite | Seedream 5 Lite
seedream-4.5 | Seedream 4.5
qwen-image-2512 | Qwen Image 2512
qwen-image | Qwen Image
qwen-image-max | Qwen Image Max
wan-2.7 | Wan 2.7
wan-2.6 | Wan 2.6
z-image-turbo | Z-Image Turbo
flux.2-klein-4b | FLUX.2 Klein 4B
flux.2-klein-9b | FLUX.2 Klein 9B
flux.2-dev-flash | FLUX.2 Dev Flash
flux.2-max | FLUX.2 Max
grok-imagine-image | Grok Imagine
grok-imagine-image-pro | Grok Imagine Pro"""


class LumenfallPipeError(RuntimeError):
    """A deliberately sanitized error safe to show in Open WebUI."""


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    display_name: str


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    content_type: str


@dataclass(frozen=True)
class GenerationMetadata:
    cost: float | None = None
    cost_currency: str | None = None
    provider: str | None = None
    provider_name: str | None = None
    executed_model: str | None = None


@dataclass(frozen=True)
class GenerationResult:
    images: list[GeneratedImage]
    metadata: GenerationMetadata


@dataclass(frozen=True)
class CostEstimate:
    model: str
    provider: str | None
    total_cost_micros: int
    currency: str


class KeyProvider(Protocol):
    def get_key(self) -> str:
        """Return the Lumenfall key or an empty string when unavailable."""


class SecretFileKeyProvider:
    """Read the key only from the fixed, read-only container secret path."""

    def __init__(self, _valves: Any = None, path: Path = LUMENFALL_SECRET_PATH):
        self._path = path

    def get_key(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, OSError):
            return ""


class PersistenceAdapter(Protocol):
    async def persist(
        self,
        *,
        image: GeneratedImage,
        filename: str,
        request: Any,
    ) -> dict[str, Any]:
        """Persist one image and return a documented files-event entry."""


def parse_model_list(value: str) -> list[ModelEntry]:
    """Parse one `model-id | Friendly name` entry per line.

    Commas are accepted as separators for simple lists. A slash in a provider-
    forced model ID is preserved. Duplicate model IDs keep their first entry.
    """

    chunks: list[str] = []
    for line in (value or "").splitlines():
        chunks.extend(line.split(","))

    result: list[ModelEntry] = []
    seen: set[str] = set()
    for chunk in chunks:
        item = chunk.strip()
        if not item or item.startswith("#"):
            continue
        model_id, separator, friendly = item.partition("|")
        model_id = model_id.strip()
        friendly = friendly.strip() if separator else model_id
        if not model_id or model_id in seen:
            continue
        if any(character.isspace() for character in model_id):
            raise ValueError(f"Invalid model ID in MODEL_LIST: {model_id!r}")
        seen.add(model_id)
        result.append(ModelEntry(model_id=model_id, display_name=friendly or model_id))
    return result


def selected_model_id(selected: str, entries: list[ModelEntry]) -> str:
    configured = {entry.model_id for entry in entries}
    if selected in configured:
        return selected
    candidate = selected.split(".", 1)[1] if "." in selected else selected
    if candidate in configured:
        return candidate
    raise LumenfallPipeError("The selected Lumenfall image model is not configured.")


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in (None, "text"):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def extract_prompt(body: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    metadata_prompt = (metadata or {}).get("user_prompt")
    prompt = _text_from_content(metadata_prompt)
    if prompt:
        return prompt

    messages = body.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            prompt = _text_from_content(message.get("content"))
            if prompt:
                return prompt
    raise LumenfallPipeError("Please provide a text prompt for image generation.")


def detect_image_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 16 and data[4:8] == b"ftyp" and data[8:12] in {
        b"avif",
        b"avis",
        b"mif1",
    }:
        return "image/avif"
    raise LumenfallPipeError("Lumenfall returned an unsupported or invalid image type.")


def decode_image(item: dict[str, Any], max_bytes: int) -> GeneratedImage:
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or not encoded.strip():
        raise LumenfallPipeError("Lumenfall returned no image data.")
    compact = "".join(encoded.split())
    maximum_encoded_length = ((max_bytes + 2) // 3) * 4
    if len(compact) > maximum_encoded_length:
        raise LumenfallPipeError("Lumenfall returned an image larger than the configured limit.")
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LumenfallPipeError("Lumenfall returned invalid base64 image data.") from exc
    if len(decoded) > max_bytes:
        raise LumenfallPipeError("Lumenfall returned an image larger than the configured limit.")

    detected = detect_image_content_type(decoded)
    declared = item.get("content_type") or item.get("mime_type")
    if declared is not None and declared != detected:
        raise LumenfallPipeError("Lumenfall returned an unexpected image content type.")
    return GeneratedImage(data=decoded, content_type=detected)


def parse_generation_response(payload: Any, max_bytes: int) -> GenerationResult:
    if not isinstance(payload, dict):
        raise LumenfallPipeError("Lumenfall returned malformed JSON.")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise LumenfallPipeError("Lumenfall returned no image data.")
    images = [decode_image(item, max_bytes) for item in data if isinstance(item, dict)]
    if len(images) != len(data) or not images:
        raise LumenfallPipeError("Lumenfall returned no image data.")

    raw_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    cost = raw_metadata.get("cost")
    metadata = GenerationMetadata(
        cost=float(cost) if isinstance(cost, (int, float)) else None,
        cost_currency=raw_metadata.get("cost_currency") if isinstance(raw_metadata.get("cost_currency"), str) else None,
        provider=raw_metadata.get("provider") if isinstance(raw_metadata.get("provider"), str) else None,
        provider_name=raw_metadata.get("provider_name")
        if isinstance(raw_metadata.get("provider_name"), str)
        else None,
        executed_model=raw_metadata.get("executed_model")
        if isinstance(raw_metadata.get("executed_model"), str)
        else None,
    )
    return GenerationResult(images=images, metadata=metadata)


def parse_cost_estimate(payload: Any) -> CostEstimate:
    if not isinstance(payload, dict) or payload.get("estimated") is not True:
        raise LumenfallPipeError("Lumenfall returned an invalid cost estimate.")
    model = payload.get("model")
    total_cost_micros = payload.get("total_cost_micros")
    currency = payload.get("currency")
    provider = payload.get("provider")
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(total_cost_micros, int)
        or isinstance(total_cost_micros, bool)
        or total_cost_micros < 0
        or not isinstance(currency, str)
        or not currency
        or (provider is not None and not isinstance(provider, str))
    ):
        raise LumenfallPipeError("Lumenfall returned an invalid cost estimate.")
    return CostEstimate(
        model=model,
        provider=provider,
        total_cost_micros=total_cost_micros,
        currency=currency,
    )


ERRORS_BY_STATUS = {
    400: "Lumenfall rejected the image request.",
    401: "Lumenfall authentication failed.",
    402: "The Lumenfall account has insufficient balance.",
    404: "The selected Lumenfall model is unavailable.",
    429: "Lumenfall is rate limiting requests; please try again later.",
    502: "All Lumenfall providers were unable to complete the request.",
}


class LumenfallClient:
    def __init__(
        self,
        *,
        key_provider: KeyProvider,
        timeout_seconds: float,
        max_image_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._key_provider = key_provider
        self._timeout_seconds = timeout_seconds
        self._max_image_bytes = max_image_bytes
        self._transport = transport

    @staticmethod
    def build_request(model: str, prompt: str, size: str = "") -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "b64_json",
        }
        if size.strip():
            request["size"] = size.strip()
        return request

    def _headers(self) -> dict[str, str]:
        api_key = self._key_provider.get_key()
        if not api_key:
            raise LumenfallPipeError("Lumenfall is not configured: no API key is available.")
        return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async def estimate(self, *, model: str, prompt: str, size: str = "") -> CostEstimate:
        """Validate and price a request without executing image generation."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    IMAGE_GENERATIONS_URL,
                    params={"dryRun": "true"},
                    headers=self._headers(),
                    json=self.build_request(model, prompt, size),
                )
        except httpx.TimeoutException as exc:
            raise LumenfallPipeError("Lumenfall timed out while estimating the image request.") from exc
        except httpx.HTTPError as exc:
            raise LumenfallPipeError("Lumenfall could not be reached.") from exc
        if response.status_code >= 400:
            raise LumenfallPipeError(
                ERRORS_BY_STATUS.get(response.status_code, "Lumenfall returned an unexpected error.")
            )
        try:
            return parse_cost_estimate(response.json())
        except ValueError as exc:
            raise LumenfallPipeError("Lumenfall returned malformed JSON.") from exc

    async def generate(self, *, model: str, prompt: str, size: str = "") -> GenerationResult:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    IMAGE_GENERATIONS_URL,
                    headers=self._headers(),
                    json=self.build_request(model, prompt, size),
                )
        except httpx.TimeoutException as exc:
            raise LumenfallPipeError("Lumenfall timed out while generating the image.") from exc
        except httpx.HTTPError as exc:
            raise LumenfallPipeError("Lumenfall could not be reached.") from exc

        if response.status_code >= 400:
            raise LumenfallPipeError(
                ERRORS_BY_STATUS.get(response.status_code, "Lumenfall returned an unexpected error.")
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LumenfallPipeError("Lumenfall returned malformed JSON.") from exc
        return parse_generation_response(payload, self._max_image_bytes)


class OpenWebUIPublicFileAdapter:
    """Persist bytes through Open WebUI's public API as the current user."""

    @staticmethod
    def _authentication(request: Any) -> tuple[dict[str, str], dict[str, str]]:
        headers: dict[str, str] = {}
        for name in ("authorization", "x-api-key"):
            value = request.headers.get(name)
            if value:
                headers[name] = value
        cookies = dict(getattr(request, "cookies", {}) or {})
        if not headers and not cookies:
            raise LumenfallPipeError(
                "Open WebUI file persistence requires the current authenticated request context."
            )
        return headers, cookies

    async def persist(
        self,
        *,
        image: GeneratedImage,
        filename: str,
        request: Any,
    ) -> dict[str, Any]:
        if request is None or getattr(request, "app", None) is None:
            raise LumenfallPipeError("Open WebUI request context is unavailable for file persistence.")
        headers, cookies = self._authentication(request)
        transport = httpx.ASGITransport(app=request.app)
        try:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://openwebui.local",
                cookies=cookies,
                timeout=30.0,
            ) as client:
                response = await client.post(
                    "/api/v1/files/?process=false",
                    headers=headers,
                    files={"file": (filename, image.data, image.content_type)},
                )
        except httpx.HTTPError as exc:
            raise LumenfallPipeError("Open WebUI could not persist the generated image.") from exc
        if response.status_code >= 400:
            raise LumenfallPipeError("Open WebUI rejected the generated image upload.")
        try:
            uploaded = response.json()
            file_id = uploaded["id"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LumenfallPipeError("Open WebUI returned an invalid file-upload response.") from exc
        name = (uploaded.get("meta") or {}).get("name") or uploaded.get("filename") or filename
        return {
            "type": "image",
            "id": file_id,
            "url": f"/api/v1/files/{file_id}/content",
            "name": name,
            "content_type": image.content_type,
        }


def _extension(content_type: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/avif": ".avif",
    }[content_type]


def _status_summary(model_name: str, metadata: GenerationMetadata) -> str:
    provider = metadata.provider_name or metadata.provider
    pieces = ["Lumenfall", model_name]
    if provider:
        pieces.append(provider)
    if metadata.cost is not None:
        currency = metadata.cost_currency or "USD"
        cost = f"{metadata.cost:.3f}"
        pieces.append(f"${cost}" if currency == "USD" else f"{cost} {currency}")
    return " · ".join(pieces)


class Pipe:
    class Valves(BaseModel):
        MODEL_LIST: str = Field(
            default=DEFAULT_MODEL_LIST,
            description="One curated image model per line: model-id | Friendly name. Commas are also accepted.",
        )
        DEFAULT_SIZE: str = Field(
            default="",
            description="Optional WIDTHxHEIGHT. Leave empty to let Lumenfall/model defaults apply.",
        )
        MAX_IMAGE_MIB: int = Field(default=25, ge=1, le=100)
        TIMEOUT_SECONDS: float = Field(default=180.0, ge=1.0, le=900.0)

    def __init__(self):
        self.valves = self.Valves()
        self._key_provider_factory: Callable[[Any], KeyProvider] = SecretFileKeyProvider
        self._persistence: PersistenceAdapter = OpenWebUIPublicFileAdapter()
        self._transport: httpx.AsyncBaseTransport | None = None

    async def pipes(self) -> list[dict[str, str]]:
        return [
            {"id": entry.model_id, "name": f"LF Image · {entry.display_name}"}
            for entry in parse_model_list(self.valves.MODEL_LIST)
        ]

    async def pipe(
        self,
        body: dict[str, Any],
        __task__: str | None = None,
        __event_emitter__: Callable[[dict[str, Any]], Any] | None = None,
        __request__: Any = None,
        __metadata__: dict[str, Any] | None = None,
    ) -> str:
        # Any named internal task is non-billable by design, including new task
        # names introduced by future Open WebUI releases.
        if __task__ is not None:
            return INTERNAL_TASK_RESPONSE
        if __event_emitter__ is None or __request__ is None:
            raise LumenfallPipeError(
                "Lumenfall image generation requires an interactive Open WebUI chat context."
            )

        entries = parse_model_list(self.valves.MODEL_LIST)
        model = selected_model_id(str(body.get("model") or ""), entries)
        model_name = next(entry.display_name for entry in entries if entry.model_id == model)
        prompt = extract_prompt(body, __metadata__)
        client = LumenfallClient(
            key_provider=self._key_provider_factory(self.valves),
            timeout_seconds=self.valves.TIMEOUT_SECONDS,
            max_image_bytes=self.valves.MAX_IMAGE_MIB * 1024 * 1024,
            transport=self._transport,
        )

        await __event_emitter__(
            {"type": "status", "data": {"description": "Creating image", "done": False}}
        )
        try:
            result = await client.generate(
                model=model,
                prompt=prompt,
                size=self.valves.DEFAULT_SIZE,
            )
            files = []
            for index, image in enumerate(result.images, start=1):
                files.append(
                    await self._persistence.persist(
                        image=image,
                        filename=f"lumenfall-image-{index}{_extension(image.content_type)}",
                        request=__request__,
                    )
                )
            await __event_emitter__({"type": "files", "data": {"files": files}})
            summary = _status_summary(model_name, result.metadata)
            await __event_emitter__(
                {"type": "status", "data": {"description": summary, "done": True}}
            )
            return "The generated image is attached to this message."
        except Exception as exc:
            safe = exc if isinstance(exc, LumenfallPipeError) else LumenfallPipeError(
                "Image generation failed unexpectedly."
            )
            await __event_emitter__(
                {"type": "status", "data": {"description": str(safe), "done": True}}
            )
            raise safe from (None if safe is exc else exc)
