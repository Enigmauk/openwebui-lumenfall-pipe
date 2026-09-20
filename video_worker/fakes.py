"""Deterministic fake collaborators for no-network worker tests."""

from __future__ import annotations

from collections import defaultdict

from .models import UpstreamJob


class FakeVideoBackend:
    def __init__(self, *, submit_error: Exception | None = None):
        self.submit_error = submit_error
        self.submit_calls = 0
        self.poll_calls = 0
        self.cancel_calls = 0
        self.submissions: dict[str, str] = {}
        self.results: dict[str, list[UpstreamJob | Exception]] = defaultdict(list)

    def set_results(self, upstream_id: str, *results: UpstreamJob | Exception) -> None:
        self.results[upstream_id] = list(results)

    async def submit(self, *, model_id: str, prompt: str, options: dict,
                     idempotency_key: str) -> UpstreamJob:
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        upstream_id = self.submissions.setdefault(idempotency_key, f"video_{len(self.submissions) + 1}")
        return UpstreamJob(upstream_id, "queued")

    async def get(self, upstream_id: str) -> UpstreamJob:
        self.poll_calls += 1
        if not self.results[upstream_id]:
            return UpstreamJob(upstream_id, "in_progress")
        result = self.results[upstream_id].pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def cancel(self, upstream_id: str) -> None:
        self.cancel_calls += 1


class FakePersistence:
    def __init__(self, *, fail_auth: bool = False):
        self.fail_auth = fail_auth
        self.calls = 0

    async def persist(self, *, job_id: str, content: bytes, mime: str,
                      credential: str) -> str:
        self.calls += 1
        if self.fail_auth:
            raise PermissionError("expired")
        return f"file_{job_id}"
