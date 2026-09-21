"""
title: Lumenfall Cost Estimator
author: Enigmauk
version: 0.1.0
required_open_webui_version: 0.11.3
description: Dry-run-only image and video cost estimates from curated Lumenfall models.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx
from pydantic import BaseModel, Field


LUMENFALL_ORIGIN = "https://api.lumenfall.ai"
IMAGE_DRY_RUN_URL = f"{LUMENFALL_ORIGIN}/openai/v1/images/generations?dryRun=true"
VIDEO_DRY_RUN_URL = f"{LUMENFALL_ORIGIN}/openai/v1/videos?dryRun=true"
LUMENFALL_SECRET_PATH = Path("/run/secrets/lumenfall-api-key")
DEFAULT_IMAGE_MODEL_LIST = """seedream-5-lite | Seedream 5 Lite
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
DEFAULT_VIDEO_MODEL_LIST = """p-video | P-Video
wan-2.6 | Wan 2.6
seedance-2.0 | Seedance 2.0
kling-v3 | Kling V3"""

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_EXECUTION_MARKERS = {
    "b64_json",
    "completed_at",
    "created_at",
    "data",
    "expires_at",
    "id",
    "image",
    "images",
    "job_id",
    "media",
    "output",
    "status",
    "upstream_id",
    "url",
}
_EFFECTIVE_PARAMETER_NAMES = {
    "aspect_ratio",
    "duration",
    "n",
    "quality",
    "resolution",
    "seconds",
    "size",
    "style",
}


class EstimationError(RuntimeError):
    """A sanitized estimation failure safe to return to the calling model."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    display_name: str


def parse_model_list(value: str) -> list[ModelEntry]:
    """Parse an administrator-curated `model-id | display name` allowlist."""
    result: list[ModelEntry] = []
    seen: set[str] = set()
    for line in (value or "").splitlines():
        for raw_item in line.split(","):
            item = raw_item.strip()
            if not item or item.startswith("#"):
                continue
            model_id, separator, display_name = item.partition("|")
            model_id = model_id.strip()
            display_name = display_name.strip() if separator else model_id
            if not _MODEL_ID.fullmatch(model_id):
                raise EstimationError("invalid_configuration", "The estimator model allowlist is invalid.")
            if model_id in seen:
                continue
            seen.add(model_id)
            result.append(ModelEntry(model_id, display_name or model_id))
    return result


class KeyProvider(Protocol):
    def get_key(self) -> str: ...


class SecretFileKeyProvider:
    """Read the API key only from the administrator-owned container secret."""

    def __init__(self, path: Path = LUMENFALL_SECRET_PATH):
        self._path = path

    def get_key(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, PermissionError, OSError):
            return ""


def _validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20_000:
        raise EstimationError(
            "invalid_request", "A prompt between 1 and 20000 characters is required."
        )
    return prompt


def _validate_option(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise EstimationError("invalid_request", f"The {name} option must be text.")
    if not value:
        return ""
    if len(value) > 64 or value != value.strip() or any(ord(character) < 32 for character in value):
        raise EstimationError("invalid_request", f"The {name} option is invalid.")
    return value


@dataclass(frozen=True)
class ImageRequestSpec:
    model: str
    prompt: str
    size: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _validate_prompt(self.prompt),
            "n": 1,
            "response_format": "b64_json",
        }
        size = _validate_option("size", self.size)
        if size:
            payload["size"] = size
        return payload


@dataclass(frozen=True)
class VideoRequestSpec:
    model: str
    prompt: str
    seconds: int | None = None
    size: str = ""
    resolution: str = ""
    aspect_ratio: str = ""

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": _validate_prompt(self.prompt),
            "n": 1,
        }
        if self.seconds is not None:
            if isinstance(self.seconds, bool) or not isinstance(self.seconds, int) or not 1 <= self.seconds <= 60:
                raise EstimationError(
                    "invalid_request", "Video seconds must be an integer between 1 and 60."
                )
            payload["seconds"] = self.seconds

        dimensions = {
            "size": _validate_option("size", self.size),
            "resolution": _validate_option("resolution", self.resolution),
            "aspect_ratio": _validate_option("aspect_ratio", self.aspect_ratio),
        }
        supplied = [name for name, value in dimensions.items() if value]
        if len(supplied) > 1:
            raise EstimationError(
                "unsupported_parameters",
                "Specify only one of size, resolution, or aspect_ratio for a video estimate.",
            )
        for name in supplied:
            payload[name] = dimensions[name]
        return payload


@dataclass(frozen=True)
class CostEstimate:
    requested_model: str
    returned_model: str
    provider: str | None
    total_cost_micros: int
    currency: str
    request: dict[str, Any]
    effective_parameters: dict[str, Any]
    components: list[dict[str, Any]]
    uses_model_defaults: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "requested_model": self.requested_model,
            "returned_model": self.returned_model,
            "provider": self.provider,
            "total_cost_micros": self.total_cost_micros,
            "currency": self.currency,
            "formatted_cost": format_cost(self.total_cost_micros, self.currency),
            "request": self.request,
            "effective_parameters": self.effective_parameters,
            "components": self.components,
            "uses_model_defaults": self.uses_model_defaults,
            "comparison_note": (
                "Optional settings were omitted, so this estimate uses model defaults and may not be directly comparable."
                if self.uses_model_defaults
                else None
            ),
        }


def format_cost(total_cost_micros: int, currency: str) -> str:
    amount = Decimal(total_cost_micros) / Decimal(1_000_000)
    text = format(amount, ".6f").rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.partition(".")[2]) == 1:
        text += "0"
    return f"${text}" if currency == "USD" else f"{text} {currency}"


def _contains_execution_marker(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in _EXECUTION_MARKERS or _contains_execution_marker(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_execution_marker(item) for item in value)
    return False


def _safe_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return not isinstance(value, str) or len(value) <= 256
    return isinstance(value, float) and math.isfinite(value)


def _parse_components(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise EstimationError("invalid_response", "Lumenfall returned an invalid cost estimate.")
    components: list[dict[str, Any]] = []
    allowed = {"type", "metric", "quantity", "billable_quantity", "unit_price", "total_cost"}
    for item in value:
        if not isinstance(item, dict):
            raise EstimationError("invalid_response", "Lumenfall returned an invalid cost estimate.")
        component = {key: item[key] for key in allowed if key in item}
        if not all(_safe_scalar(component_value) for component_value in component.values()):
            raise EstimationError("invalid_response", "Lumenfall returned an invalid cost estimate.")
        components.append(component)
    return components


def parse_estimate_response(
    payload: Any,
    *,
    requested_model: str,
    request: dict[str, Any],
    optional_parameter_names: tuple[str, ...],
) -> CostEstimate:
    if not isinstance(payload, dict):
        raise EstimationError("invalid_response", "Lumenfall returned malformed estimate JSON.")
    if _contains_execution_marker(payload):
        raise EstimationError(
            "unexpected_execution_response",
            "Lumenfall returned execution or media data to a dry-run request; estimation stopped.",
        )
    if payload.get("estimated") is not True:
        raise EstimationError(
            "estimate_not_confirmed", "Lumenfall did not confirm that the response was an estimate."
        )

    returned_model = payload.get("model")
    provider = payload.get("provider")
    total_cost_micros = payload.get("total_cost_micros")
    currency = payload.get("currency")
    if (
        not isinstance(returned_model, str)
        or not returned_model
        or len(returned_model) > 256
        or (provider is not None and (not isinstance(provider, str) or not provider or len(provider) > 128))
        or not isinstance(total_cost_micros, int)
        or isinstance(total_cost_micros, bool)
        or total_cost_micros < 0
        or not isinstance(currency, str)
        or not _CURRENCY.fullmatch(currency)
    ):
        raise EstimationError("invalid_response", "Lumenfall returned an invalid cost estimate.")

    effective_parameters: dict[str, Any] = {}
    raw_effective = payload.get("effective_parameters")
    if raw_effective is not None:
        if not isinstance(raw_effective, dict):
            raise EstimationError("invalid_response", "Lumenfall returned an invalid cost estimate.")
        for name, value in raw_effective.items():
            if name in _EFFECTIVE_PARAMETER_NAMES and _safe_scalar(value):
                effective_parameters[name] = value
    for name in _EFFECTIVE_PARAMETER_NAMES:
        if name in payload and _safe_scalar(payload[name]):
            effective_parameters[name] = payload[name]

    uses_model_defaults = not any(name in request for name in optional_parameter_names)
    return CostEstimate(
        requested_model=requested_model,
        returned_model=returned_model,
        provider=provider,
        total_cost_micros=total_cost_micros,
        currency=currency,
        request=request,
        effective_parameters=effective_parameters,
        components=_parse_components(payload.get("components")),
        uses_model_defaults=uses_model_defaults,
    )


_HTTP_ERRORS = {
    400: ("request_rejected", "Lumenfall rejected the requested model or parameters."),
    401: ("authentication_failed", "Lumenfall authentication failed."),
    402: ("insufficient_balance", "The Lumenfall account has insufficient balance."),
    403: ("access_denied", "Lumenfall denied the estimate request."),
    404: ("model_unavailable", "The selected Lumenfall model is unavailable."),
    429: ("rate_limited", "Lumenfall is rate limiting estimate requests; try again later."),
    502: ("provider_unavailable", "Lumenfall providers could not estimate the request."),
}


class DryRunEstimator:
    """Fixed-origin client that can only submit dry-run estimate requests."""

    def __init__(
        self,
        *,
        key_provider: KeyProvider,
        timeout_seconds: float = 30.0,
        max_response_bytes: int = 128 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._key_provider = key_provider
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        try:
            key = self._key_provider.get_key()
        except Exception:
            raise EstimationError("key_unavailable", "The Lumenfall API key is unavailable.") from None
        if not isinstance(key, str) or not key or "\r" in key or "\n" in key:
            raise EstimationError("key_unavailable", "The Lumenfall API key is unavailable.")
        return {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def estimate_image(self, spec: ImageRequestSpec) -> CostEstimate:
        request = spec.to_payload()
        payload = await self._post_dry_run(IMAGE_DRY_RUN_URL, request)
        return parse_estimate_response(
            payload,
            requested_model=spec.model,
            request=request,
            optional_parameter_names=("size",),
        )

    async def estimate_video(self, spec: VideoRequestSpec) -> CostEstimate:
        request = spec.to_payload()
        payload = await self._post_dry_run(VIDEO_DRY_RUN_URL, request)
        return parse_estimate_response(
            payload,
            requested_model=spec.model,
            request=request,
            optional_parameter_names=("seconds", "size", "resolution", "aspect_ratio"),
        )

    async def _post_dry_run(self, url: str, request: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(self._timeout_seconds, connect=min(10.0, self._timeout_seconds))
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self._transport,
                follow_redirects=False,
                trust_env=False,
                verify=True,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
            ) as client:
                async with client.stream(
                    "POST", url, headers=self._headers(), json=request
                ) as response:
                    if response.status_code == 202:
                        raise EstimationError(
                            "unexpected_execution_response",
                            "Lumenfall accepted a job for a dry-run request; estimation stopped.",
                        )
                    if response.status_code != 200:
                        code, message = _HTTP_ERRORS.get(
                            response.status_code,
                            ("upstream_error", "Lumenfall returned an unexpected estimate error."),
                        )
                        raise EstimationError(code, message)
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared) < 0 or int(declared) > self._max_response_bytes:
                                raise EstimationError(
                                    "response_too_large", "Lumenfall returned an oversized estimate response."
                                )
                        except ValueError:
                            raise EstimationError(
                                "invalid_response", "Lumenfall returned an invalid estimate response."
                            ) from None
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                        if len(body) + len(chunk) > self._max_response_bytes:
                            raise EstimationError(
                                "response_too_large", "Lumenfall returned an oversized estimate response."
                            )
                        body.extend(chunk)
        except EstimationError:
            raise
        except httpx.TimeoutException:
            raise EstimationError("timeout", "Lumenfall timed out while estimating the request.") from None
        except httpx.HTTPError:
            raise EstimationError("network_error", "Lumenfall could not be reached for an estimate.") from None

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EstimationError("invalid_response", "Lumenfall returned malformed estimate JSON.") from None
        if not isinstance(payload, dict):
            raise EstimationError("invalid_response", "Lumenfall returned malformed estimate JSON.")
        return payload


def _error_result(model: str, exc: EstimationError) -> dict[str, Any]:
    return {"ok": False, "requested_model": model, "error": {"code": exc.code, "message": str(exc)}}


def _unexpected_error_result(model: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        **({"requested_model": model} if model else {}),
        "error": {
            "code": "internal_error",
            "message": "The estimate failed unexpectedly without generating media.",
        },
    }


class Tools:
    class Valves(BaseModel):
        IMAGE_MODEL_LIST: str = Field(
            default=DEFAULT_IMAGE_MODEL_LIST,
            description="Administrator-curated image models: model-id | display name.",
        )
        VIDEO_MODEL_LIST: str = Field(
            default=DEFAULT_VIDEO_MODEL_LIST,
            description="Administrator-curated video models: model-id | display name.",
        )
        MAX_MODELS_PER_COMPARISON: int = Field(default=5, ge=1, le=5)
        TIMEOUT_SECONDS: float = Field(default=30.0, ge=1.0, le=120.0)
        MAX_RESPONSE_KIB: int = Field(default=128, ge=16, le=512)

    def __init__(self):
        self.valves = self.Valves()
        self._key_provider_factory: Callable[[], KeyProvider] = SecretFileKeyProvider
        self._transport: httpx.AsyncBaseTransport | None = None

    def _entries(self, media_type: str) -> list[ModelEntry]:
        if media_type == "image":
            return parse_model_list(self.valves.IMAGE_MODEL_LIST)
        if media_type == "video":
            return parse_model_list(self.valves.VIDEO_MODEL_LIST)
        raise EstimationError("invalid_request", "media_type must be image or video.")

    def _require_model(self, media_type: str, model: str) -> None:
        allowed = {entry.model_id for entry in self._entries(media_type)}
        if model not in allowed:
            raise EstimationError(
                "model_not_allowed", f"The requested {media_type} model is not in the curated allowlist."
            )

    def _require_models(self, media_type: str, models: list[str]) -> None:
        if not isinstance(models, list) or not models:
            raise EstimationError("invalid_request", "Provide at least one model to compare.")
        if len(models) > self.valves.MAX_MODELS_PER_COMPARISON:
            raise EstimationError(
                "too_many_models",
                f"A comparison supports at most {self.valves.MAX_MODELS_PER_COMPARISON} models.",
            )
        if len(set(models)) != len(models):
            raise EstimationError("invalid_request", "Comparison model IDs must be distinct.")
        for model in models:
            self._require_model(media_type, model)

    def _estimator(self) -> DryRunEstimator:
        return DryRunEstimator(
            key_provider=self._key_provider_factory(),
            timeout_seconds=self.valves.TIMEOUT_SECONDS,
            max_response_bytes=self.valves.MAX_RESPONSE_KIB * 1024,
            transport=self._transport,
        )

    async def estimate_image(self, prompt: str, model: str, size: str = "") -> dict[str, Any]:
        """Estimate only; never generate. Use for an explicit image cost, price, or quote request."""
        try:
            self._require_model("image", model)
            result = await self._estimator().estimate_image(ImageRequestSpec(model, prompt, size))
            return result.to_dict()
        except EstimationError as exc:
            return _error_result(model, exc)
        except Exception:
            return _unexpected_error_result(model)

    async def compare_image_models(
        self, prompt: str, models: list[str], size: str = ""
    ) -> dict[str, Any]:
        """Estimate the same explicit image request across curated models; never generate media."""
        try:
            self._require_models("image", models)
        except EstimationError as exc:
            return {"ok": False, "results": [], "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            return {**_unexpected_error_result(), "results": []}
        estimator = self._estimator()
        results: list[dict[str, Any]] = []
        stopped_early = False
        for model in models:
            try:
                estimate = await estimator.estimate_image(ImageRequestSpec(model, prompt, size))
                results.append(estimate.to_dict())
            except EstimationError as exc:
                results.append(_error_result(model, exc))
                if exc.code == "unexpected_execution_response":
                    stopped_early = True
                    break
            except Exception:
                results.append(_unexpected_error_result(model))
        return {
            "ok": not stopped_early and any(result["ok"] for result in results),
            "media_type": "image",
            "stopped_early": stopped_early,
            "results": results,
        }

    async def estimate_video(
        self,
        prompt: str,
        model: str,
        seconds: int | None = None,
        size: str = "",
        resolution: str = "",
        aspect_ratio: str = "",
    ) -> dict[str, Any]:
        """Estimate only; never generate. Use for an explicit video cost, price, or quote request."""
        try:
            self._require_model("video", model)
            spec = VideoRequestSpec(model, prompt, seconds, size, resolution, aspect_ratio)
            result = await self._estimator().estimate_video(spec)
            return result.to_dict()
        except EstimationError as exc:
            return _error_result(model, exc)
        except Exception:
            return _unexpected_error_result(model)

    async def compare_video_models(
        self,
        prompt: str,
        models: list[str],
        seconds: int | None = None,
        size: str = "",
        resolution: str = "",
        aspect_ratio: str = "",
    ) -> dict[str, Any]:
        """Estimate the same explicit video request across curated models; never generate media."""
        try:
            self._require_models("video", models)
            # Validate shared options before any model request is made.
            VideoRequestSpec(models[0], prompt, seconds, size, resolution, aspect_ratio).to_payload()
        except EstimationError as exc:
            return {"ok": False, "results": [], "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            return {**_unexpected_error_result(), "results": []}
        estimator = self._estimator()
        results: list[dict[str, Any]] = []
        stopped_early = False
        for model in models:
            try:
                spec = VideoRequestSpec(model, prompt, seconds, size, resolution, aspect_ratio)
                estimate = await estimator.estimate_video(spec)
                results.append(estimate.to_dict())
            except EstimationError as exc:
                results.append(_error_result(model, exc))
                if exc.code == "unexpected_execution_response":
                    stopped_early = True
                    break
            except Exception:
                results.append(_unexpected_error_result(model))
        return {
            "ok": not stopped_early and any(result["ok"] for result in results),
            "media_type": "video",
            "stopped_early": stopped_early,
            "results": results,
        }

    async def list_estimate_models(self, media_type: str) -> dict[str, Any]:
        """List exact curated model IDs available for image or video estimates; makes no network request."""
        try:
            entries = self._entries(media_type)
            return {
                "ok": True,
                "media_type": media_type,
                "models": [
                    {"model": entry.model_id, "display_name": entry.display_name} for entry in entries
                ],
            }
        except EstimationError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        except Exception:
            return _unexpected_error_result()
