"""Explicit construction path for HTTP collaborators; development defaults stay fake."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

import httpx

from .db import JobStore
from .downloader import SecureResultDownloader
from .lumenfall_http import LumenfallKeyProvider, LumenfallVideoClient
from .security import SecretBox
from .service import Persistence, WorkerService


def create_http_worker_service(
    *,
    store: JobStore,
    persistence: Persistence,
    secret_box: SecretBox,
    fingerprint_key: bytes,
    key_provider: LumenfallKeyProvider,
    artifact_directory: str | Path,
    api_transport: httpx.AsyncBaseTransport | None = None,
    download_transport: httpx.AsyncBaseTransport | None = None,
    resolver: Callable[[str, int], Iterable[str]] | None = None,
) -> WorkerService:
    """Build real HTTP collaborators only when explicitly selected by a caller.

    Production service defaults are not changed; tests and the current worker
    state-machine wiring can continue to inject deterministic fakes.
    """
    backend = LumenfallVideoClient(key_provider, transport=api_transport)
    downloader = SecureResultDownloader(
        artifact_directory,
        transport=download_transport,
        resolver=resolver,
    )
    return WorkerService(
        store, backend, persistence, secret_box, fingerprint_key,
        artifact_directory=artifact_directory, downloader=downloader,
    )
