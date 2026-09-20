import os
import tempfile
import time
import unittest
from pathlib import Path

import httpx

from video_worker.app import create_app
from video_worker.db import DuplicateRequestConflict, InvalidTransition, JobStore
from video_worker.fakes import FakePersistence, FakeSavedChatVerifier, FakeVideoBackend
from video_worker.models import AmbiguousSubmit, UpstreamJob, WorkerState
from video_worker.security import SecretBox
from video_worker.service import WorkerService


MP4 = b"\x00\x00\x00\x18ftypisom" + b"mock-video"


class WorkerFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "worker.sqlite3"
        self.backend = FakeVideoBackend()
        self.persistence = FakePersistence()
        self.saved_chat_verifier = FakeSavedChatVerifier()
        self.store = JobStore(self.db_path)
        self.box = SecretBox(b"e" * 32)
        self.service = WorkerService(
            self.store, self.backend, self.persistence, self.box, b"f" * 32,
            saved_chat_verifier=self.saved_chat_verifier,
        )

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def create(self, **changes):
        values = {
            "owner_user_id": "user-1",
            "chat_id": "chat-1",
            "assistant_message_id": "message-1",
            "friendly_model": "P-Video",
            "model_id": "p-video",
            "prompt": "A red kite over green hills",
            "options": {"seconds": 5},
            "user_credential": "fake-current-user-jwt",
        }
        values.update(changes)
        return self.service.create_job(**values)


class StoreTests(WorkerFixture, unittest.TestCase):
    def test_creation_is_durable_and_secrets_are_ciphertext(self):
        job, created = self.create()
        self.assertTrue(created)
        self.assertEqual(job.worker_state, WorkerState.PENDING_SUBMIT)
        self.assertTrue(job.idempotency_key.startswith("lumenfall-video:"))
        self.assertNotIn(b"red kite", job.request_ciphertext)
        self.assertNotIn(b"fake-current", job.credential_ciphertext)
        self.store.close()
        self.store = JobStore(self.db_path)
        persisted = self.store.get(job.job_id)
        self.assertEqual(persisted.idempotency_key, job.idempotency_key)
        self.assertEqual(persisted.request_fingerprint, job.request_fingerprint)

    def test_duplicate_logical_request_returns_existing_job(self):
        first, _ = self.create()
        second, created = self.create()
        self.assertFalse(created)
        self.assertEqual(second.job_id, first.job_id)

    def test_changed_duplicate_is_conflict(self):
        self.create()
        with self.assertRaises(DuplicateRequestConflict):
            self.create(prompt="A different paid intent")

    def test_claim_is_exactly_once_and_committed_before_submit(self):
        job, _ = self.create()
        claimed = self.store.claim_submission(job.job_id)
        self.assertEqual(claimed.worker_state, WorkerState.SUBMITTING)
        self.assertEqual(claimed.submit_attempts, 1)
        self.assertIsNone(self.store.claim_submission(job.job_id))

    def test_invalid_transition_rejected(self):
        job, _ = self.create()
        with self.assertRaises(InvalidTransition):
            self.store.transition(job.job_id, WorkerState.COMPLETED)

    def test_restart_marks_inflight_submit_ambiguous(self):
        job, _ = self.create()
        self.store.claim_submission(job.job_id)
        resumable = self.store.recover()
        recovered = self.store.get(job.job_id)
        self.assertEqual(recovered.worker_state, WorkerState.SUBMIT_AMBIGUOUS)
        self.assertIsNone(recovered.request_ciphertext)
        self.assertIsNone(recovered.credential_ciphertext)
        self.assertNotIn(job.job_id, [item.job_id for item in resumable])


class LifecycleTests(WorkerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_queued_in_progress_completed_and_persisted(self):
        job, _ = self.create()
        await self.service.advance(job.job_id)
        self.backend.set_results(
            "video_1",
            UpstreamJob("video_1", "queued"),
            UpstreamJob("video_1", "in_progress"),
            UpstreamJob("video_1", "completed", "fake", "fake/model", 1250,
                        "USD", "video/mp4", MP4),
        )
        final = await self.service.run_until_stable(job.job_id)
        self.assertEqual(final.worker_state, WorkerState.COMPLETED)
        self.assertEqual(final.openwebui_file_id, f"file_{job.job_id}")
        self.assertEqual(final.final_cost_micros, 1250)
        self.assertIsNone(final.credential_ciphertext)
        self.assertEqual(self.backend.submit_calls, 1)

    async def test_upstream_failure_is_terminal_and_durable(self):
        job, _ = self.create()
        await self.service.advance(job.job_id)
        self.backend.set_results("video_1", UpstreamJob("video_1", "failed", error_code="MOCK_FAILED"))
        failed = await self.service.advance(job.job_id)
        self.assertEqual(failed.worker_state, WorkerState.FAILED)
        self.assertEqual(failed.last_safe_error, "MOCK_FAILED")
        self.assertIsNone(failed.credential_ciphertext)

    async def test_transient_poll_error_then_success(self):
        job, _ = self.create()
        await self.service.advance(job.job_id)
        self.backend.set_results(
            "video_1", TimeoutError(),
            UpstreamJob("video_1", "completed", output_mime="video/mp4", output_bytes=MP4),
        )
        interrupted = await self.service.advance(job.job_id)
        self.assertEqual(interrupted.worker_state, WorkerState.POLL_INTERRUPTED)
        final = await self.service.run_until_stable(job.job_id)
        self.assertEqual(final.worker_state, WorkerState.COMPLETED)
        self.assertEqual(self.backend.submit_calls, 1)

    async def test_ambiguous_submit_is_never_retried(self):
        self.backend.submit_error = AmbiguousSubmit()
        job, _ = self.create()
        ambiguous = await self.service.advance(job.job_id)
        self.assertEqual(ambiguous.worker_state, WorkerState.SUBMIT_AMBIGUOUS)
        await self.service.advance(job.job_id)
        self.assertEqual(self.backend.submit_calls, 1)
        self.assertIsNone(ambiguous.request_ciphertext)

    async def test_restart_after_submit_resumes_polling(self):
        job, _ = self.create()
        submitted = await self.service.advance(job.job_id)
        self.assertEqual(submitted.worker_state, WorkerState.QUEUED)
        self.store.close()
        self.store = JobStore(self.db_path)
        self.service = WorkerService(
            self.store, self.backend, self.persistence, self.box, b"f" * 32,
            saved_chat_verifier=self.saved_chat_verifier,
        )
        recovered = self.store.recover()
        self.assertEqual([item.job_id for item in recovered], [job.job_id])
        self.backend.set_results("video_1", UpstreamJob("video_1", "in_progress"))
        polled = await self.service.advance(job.job_id)
        self.assertEqual(polled.worker_state, WorkerState.IN_PROGRESS)
        self.assertEqual(self.backend.submit_calls, 1)

    async def test_restart_before_persistence_resumes_without_submit(self):
        job, _ = self.create()
        await self.service.advance(job.job_id)
        self.backend.set_results(
            "video_1", UpstreamJob("video_1", "completed", output_mime="video/mp4", output_bytes=MP4)
        )
        downloading = await self.service.advance(job.job_id)
        self.assertEqual(downloading.worker_state, WorkerState.DOWNLOADING)
        self.store.close()
        self.store = JobStore(self.db_path)
        self.service = WorkerService(
            self.store, self.backend, self.persistence, self.box, b"f" * 32,
            saved_chat_verifier=self.saved_chat_verifier,
        )
        self.store.recover()
        final = await self.service.run_until_stable(job.job_id)
        self.assertEqual(final.worker_state, WorkerState.COMPLETED)
        self.assertEqual(self.backend.submit_calls, 1)

    async def test_cancellation_is_persisted_and_best_effort(self):
        job, _ = self.create()
        await self.service.advance(job.job_id)
        cancelled = await self.service.request_cancel(job.job_id, "user-1")
        self.assertEqual(cancelled.worker_state, WorkerState.CANCEL_REQUESTED)
        self.assertTrue(cancelled.cancel_requested)
        self.backend.set_results("video_1", UpstreamJob("video_1", "in_progress"))
        observed = await self.service.advance(job.job_id)
        self.assertEqual(observed.worker_state, WorkerState.IN_PROGRESS)
        self.assertEqual(self.backend.cancel_calls, 1)

    async def test_delivery_auth_failure_preserves_result_for_refresh(self):
        self.persistence.fail_auth = True
        job, _ = self.create()
        await self.service.advance(job.job_id)
        self.backend.set_results(
            "video_1", UpstreamJob("video_1", "completed", output_mime="video/mp4", output_bytes=MP4)
        )
        waiting = await self.service.run_until_stable(job.job_id)
        self.assertEqual(waiting.worker_state, WorkerState.DELIVERY_AUTH_REQUIRED)
        self.assertIsNotNone(waiting.artifact_path)
        self.persistence.fail_auth = False
        refreshed = self.service.refresh_credential(job.job_id, "user-1", "fresh-fake-jwt")
        self.assertEqual(refreshed.worker_state, WorkerState.PERSISTING)
        final = await self.service.run_until_stable(job.job_id)
        self.assertEqual(final.worker_state, WorkerState.COMPLETED)


class ApiTests(WorkerFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.app = create_app(self.service, "test-worker-secret")
        self.transport = httpx.ASGITransport(app=self.app)
        self.headers = {
            "Authorization": "Bearer test-worker-secret",
            "X-OpenWebUI-User-Id": "user-1",
        }
        self.payload = {
            "chat_id": "chat-api", "assistant_message_id": "message-api",
            "friendly_model": "P-Video", "model_id": "p-video",
            "prompt": "A mock video", "options": {},
            "user_credential": "fake-jwt",
        }

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(transport=self.transport, base_url="http://worker") as client:
            return await client.request(method, path, **kwargs)

    async def test_auth_required_and_invalid_auth_rejected(self):
        self.assertEqual((await self.request("GET", "/health")).status_code, 401)
        self.assertEqual((await self.request("GET", "/health", headers={"Authorization": "Bearer wrong"})).status_code, 401)

    async def test_health_contains_no_job_details(self):
        response = await self.request("GET", "/health", headers={"Authorization": "Bearer test-worker-secret"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": True})

    async def test_create_status_duplicate_and_cross_user_isolation(self):
        created = await self.request("POST", "/jobs", headers=self.headers, json=self.payload)
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["job_id"]
        duplicate = await self.request("POST", "/jobs", headers=self.headers, json=self.payload)
        self.assertFalse(duplicate.json()["created"])
        self.assertEqual(self.backend.submit_calls, 1)
        own = await self.request("GET", f"/jobs/{job_id}", headers=self.headers)
        self.assertEqual(own.status_code, 200)
        other_headers = {**self.headers, "X-OpenWebUI-User-Id": "user-2"}
        other = await self.request("GET", f"/jobs/{job_id}", headers=other_headers)
        self.assertEqual(other.status_code, 404)
        cancel = await self.request("DELETE", f"/jobs/{job_id}", headers=other_headers)
        self.assertEqual(cancel.status_code, 404)
        refresh = await self.request(
            "POST", f"/jobs/{job_id}/delivery-credential", headers=other_headers,
            json={"user_credential": "other-fake-jwt"},
        )
        self.assertEqual(refresh.status_code, 404)

    async def test_changed_duplicate_returns_conflict(self):
        await self.request("POST", "/jobs", headers=self.headers, json=self.payload)
        changed = {**self.payload, "prompt": "different"}
        response = await self.request("POST", "/jobs", headers=self.headers, json=changed)
        self.assertEqual(response.status_code, 409)
