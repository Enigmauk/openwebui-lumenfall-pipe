"""Mock-only end-to-end tests for the development video Pipe/worker boundary."""

from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

from lumenfall_video_pipe import (
    MAX_WORKER_RESPONSE_BYTES,
    Pipe,
    VideoPipeError,
    VideoWorkerClient,
    WorkerClientError,
    current_user_credential,
)
from video_worker.app import create_app
from video_worker.db import JobStore
from video_worker.downloader import SecureResultDownloader
from video_worker.fakes import FakePersistence, FakeSavedChatVerifier, FakeVideoBackend
from video_worker.models import UpstreamJob, WorkerState
from video_worker.security import SecretBox
from video_worker.service import WorkerService


FAKE_WORKER_BEARER = "fake-worker-only-bearer-for-test"
FAKE_USER_JWT = "fake-current-user-jwt-for-test"
FAKE_SIGNED_QUERY = "fake-result-signature-for-test"
OWNER = "user-1"
CHAT = "saved-chat-1"
MESSAGE = "saved-assistant-message-1"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"mock-video"


class FakeBearerProvider:
    def __init__(self, token: str = FAKE_WORKER_BEARER):
        self.token = token
        self.reads = 0

    def get_token(self) -> str:
        self.reads += 1
        return self.token


class WorkerASGITransport(httpx.AsyncBaseTransport):
    """ASGI worker transport with an independent mocked worker scheduler."""

    def __init__(self, app, service: WorkerService, *, advance_on_get: bool = True):
        self._asgi = httpx.ASGITransport(app=app)
        self._service = service
        self._advance_on_get = advance_on_get
        self.requests: list[tuple[str, str, dict[str, str], bytes]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        self.requests.append((request.method, request.url.path, dict(request.headers), content))
        if self._advance_on_get and request.method == "GET" and request.url.path.startswith("/jobs/"):
            job_id = request.url.path.rsplit("/", 1)[-1]
            await self._service.run_until_stable(job_id)
        replay = httpx.Request(
            request.method, request.url, headers=request.headers, content=content,
        )
        return await self._asgi.handle_async_request(replay)

    async def aclose(self) -> None:
        await self._asgi.aclose()


class TestRequest:
    def __init__(self, *, bearer: str | None = None,
                 cookies: dict[str, str] | None = None):
        self.headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
        self.cookies = cookies or {}


class VideoPipeWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloaders: list[SecureResultDownloader] = []
        self.stores: list[JobStore] = []

    async def asyncTearDown(self):
        for downloader in self.downloaders:
            await downloader.aclose()
        for store in self.stores:
            try:
                store.close()
            except Exception:
                pass
        self.temp.cleanup()

    def make_worker(self, *, submit_error=None, persistence=None,
                    saved_messages=None, download_body=MP4,
                    worker_secret=FAKE_WORKER_BEARER):
        db_path = self.root / f"worker-{len(self.stores)}.sqlite3"
        store = JobStore(db_path)
        self.stores.append(store)
        backend = FakeVideoBackend(submit_error=submit_error)
        persistence = persistence or FakePersistence()
        verifier = FakeSavedChatVerifier(saved_messages)
        download_requests: list[httpx.Request] = []

        def download(request):
            download_requests.append(request)
            return httpx.Response(
                200, headers={"content-type": "video/mp4"}, content=download_body,
            )

        downloader = SecureResultDownloader(
            self.root / f"artifacts-{len(self.stores)}",
            transport=httpx.MockTransport(download),
            resolver=lambda _host, _port: ["8.8.8.8"],
        )
        self.downloaders.append(downloader)
        service = WorkerService(
            store, backend, persistence, SecretBox(b"e" * 32), b"f" * 32,
            artifact_directory=downloader.artifact_directory,
            downloader=downloader,
            saved_chat_verifier=verifier,
        )
        app = create_app(service, worker_secret)
        return service, backend, persistence, verifier, app, download_requests

    def make_pipe(self, app, service, *, token=FAKE_WORKER_BEARER,
                  advance_on_get=True):
        pipe = Pipe()
        pipe.valves.MAX_STATUS_CHECKS = 2
        transports: list[WorkerASGITransport] = []
        provider = FakeBearerProvider(token)

        def factory():
            transport = WorkerASGITransport(app, service, advance_on_get=advance_on_get)
            transports.append(transport)
            return VideoWorkerClient(provider, transport=transport)

        pipe._worker_client_factory = factory
        pipe._sleep = _no_sleep
        return pipe, provider, transports

    @staticmethod
    def request_context(*, user_id=OWNER, chat_id=CHAT, message_id=MESSAGE,
                        bearer=FAKE_USER_JWT):
        return {
            "user": {"id": user_id},
            "metadata": {"chat_id": chat_id, "message_id": message_id},
            "message_id": message_id,
            "request": TestRequest(bearer=bearer),
        }

    @staticmethod
    def body(prompt="A small synthetic test video"):
        return {
            "model": "lumenfall_video_pipe.video:p-video",
            "messages": [{"role": "user", "content": prompt}],
            "video_options": {"seconds": 5},
            # This adversarial field must never become the job owner.
            "owner_user_id": "attacker-selected-owner",
        }

    async def test_catalog_is_separate_video_only_and_admin_curatable(self):
        pipe = Pipe()
        selectors = await pipe.pipes()
        self.assertEqual([entry["id"] for entry in selectors], [
            "video:p-video", "video:wan-2.6", "video:seedance-2.0", "video:kling-v3",
        ])
        self.assertTrue(all(entry["name"].startswith("LF Video · ") for entry in selectors))
        pipe.valves.VIDEO_MODEL_LIST = "mock-video | Curated Mock\ninvalid model id | No"
        self.assertEqual(await pipe.pipes(), [{"id": "video:mock-video", "name": "LF Video · Curated Mock"}])

    async def test_any_non_null_internal_task_exits_before_reading_inputs_or_secrets(self):
        pipe = Pipe()
        factory_calls = []
        pipe._worker_client_factory = lambda: factory_calls.append("called")

        class ExplosiveDict(dict):
            def get(self, *_args, **_kwargs):
                raise AssertionError("internal-task path read the body")

        for task in ("title_generation", "future_unknown_task", ""):
            result = await pipe.pipe(
                ExplosiveDict(), __task__=task, __user__=object(),
                __metadata__=object(), __request__=object(),
                __event_emitter__=object(),
            )
            self.assertEqual(result, "")
        self.assertEqual(factory_calls, [])

    async def test_missing_or_temporary_saved_message_fails_before_worker_request(self):
        service, _, _, _, app, _ = self.make_worker()
        pipe, provider, transports = self.make_pipe(app, service)
        events = []

        async def emit(event):
            events.append(event)

        invalid_contexts = (
            ({"chat_id": "new-chat"}, "new-message"),
            ({}, None),
            ({"chat_id": CHAT}, None),
        )
        for metadata, message_id in invalid_contexts:
            with self.subTest(metadata=metadata, message_id=message_id):
                with self.assertRaisesRegex(VideoPipeError, "Save this chat message"):
                    await pipe.pipe(self.body(), __user__={"id": OWNER},
                                    __metadata__=metadata, __message_id__=message_id,
                                    __request__=TestRequest(), __event_emitter__=emit)
        self.assertEqual(provider.reads, 0)
        self.assertEqual(transports, [])

    async def test_unknown_selector_and_invalid_options_fail_before_worker_client(self):
        service, _, _, _, app, _ = self.make_worker()
        pipe, provider, transports = self.make_pipe(app, service)
        context = self.request_context()
        unknown = self.body()
        unknown["model"] = "lumenfall_video_pipe.video:not-curated"
        with self.assertRaisesRegex(VideoPipeError, "not available"):
            await pipe.pipe(unknown, __user__=context["user"],
                            __metadata__=context["metadata"], __message_id__=MESSAGE,
                            __request__=context["request"], __event_emitter__=_event_sink)
        invalid_options = self.body()
        invalid_options["video_options"] = {"seconds": 600}
        with self.assertRaisesRegex(VideoPipeError, "between 1 and 60"):
            await pipe.pipe(invalid_options, __user__=context["user"],
                            __metadata__=context["metadata"], __message_id__=MESSAGE,
                            __request__=context["request"], __event_emitter__=_event_sink)
        self.assertEqual(provider.reads, 0)
        self.assertEqual(transports, [])

    async def test_worker_auth_failure_is_sanitized(self):
        service, _, _, _, app, _ = self.make_worker()
        pipe, provider, _ = self.make_pipe(app, service, token="wrong-fake-worker-token")
        context = self.request_context()
        with self.assertRaises(WorkerClientError) as caught:
            await pipe.pipe(self.body(), __user__=context["user"],
                            __metadata__=context["metadata"],
                            __message_id__=context["message_id"],
                            __request__=context["request"], __event_emitter__=_event_sink)
        self.assertEqual(str(caught.exception), "WORKER_AUTH_REJECTED")
        self.assertNotIn("wrong-fake", str(caught.exception))
        self.assertEqual(provider.reads, 1)

    async def test_pipe_worker_fake_backend_downloader_persistence_and_restart(self):
        service, backend, persistence, verifier, app, downloads = self.make_worker(
            saved_messages={(OWNER, CHAT, MESSAGE)},
        )
        signed_url = f"https://result.invalid/final.mp4?signature={FAKE_SIGNED_QUERY}"
        backend.set_results(
            "video_1",
            UpstreamJob("video_1", "queued"),
            UpstreamJob("video_1", "in_progress"),
            UpstreamJob(
                "video_1", "completed", provider="fake-provider",
                executed_model="fake/video-v1", cost_micros=1200,
                cost_currency="USD", output_mime="video/mp4",
                output_url=signed_url, expected_bytes=len(MP4),
            ),
        )
        pipe, provider, transports = self.make_pipe(app, service)
        context = self.request_context()
        events = []

        async def emit(event):
            events.append(event)

        result = await pipe.pipe(
            self.body(), __user__=context["user"], __metadata__=context["metadata"],
            __message_id__=context["message_id"], __request__=context["request"],
            __event_emitter__=emit,
        )
        job_id = service.store._db.execute("SELECT job_id FROM jobs").fetchone()[0]
        job = service.owned(job_id, OWNER)
        self.assertIn("completed", result)
        self.assertEqual(job.worker_state, WorkerState.COMPLETED)
        self.assertEqual(backend.submit_calls, 1)
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0].url.host, "result.invalid")
        self.assertEqual(persistence.calls, 1)
        self.assertEqual(persistence.records[0]["content"], MP4)
        self.assertEqual(
            (persistence.records[0]["owner_user_id"], persistence.records[0]["chat_id"],
             persistence.records[0]["assistant_message_id"]),
            (OWNER, CHAT, MESSAGE),
        )
        self.assertEqual(verifier.calls, [(OWNER, CHAT, MESSAGE)])
        self.assertEqual(provider.reads, 2)  # create and one bounded status GET
        post_requests = [entry for transport in transports for entry in transport.requests
                         if entry[0] == "POST" and entry[1] == "/jobs"]
        self.assertEqual(len(post_requests), 1)
        post_body = json.loads(post_requests[0][3])
        self.assertEqual(post_body["user_credential"], FAKE_USER_JWT)
        self.assertNotIn("owner_user_id", post_body)
        self.assertEqual(post_requests[0][2]["x-openwebui-user-id"], OWNER)
        self.assertEqual(post_requests[0][2]["authorization"], f"Bearer {FAKE_WORKER_BEARER}")
        self.assertNotIn("cookie", post_requests[0][2])
        self.assertTrue(job.idempotency_key.startswith("lumenfall-video:"))
        self.assertEqual(job.submit_attempts, 1)
        self.assertNotIn(FAKE_WORKER_BEARER, json.dumps(events))
        self.assertNotIn(FAKE_USER_JWT, json.dumps(events))
        self.assertNotIn(FAKE_SIGNED_QUERY, result + json.dumps(events))
        files = [event["data"]["files"][0] for event in events if event["type"] == "files"]
        self.assertEqual(files[0]["type"], "file")
        self.assertEqual(files[0]["id"], job.openwebui_file_id)
        self.assertIn("/api/v1/files/", files[0]["url"])
        self.assertNotIn("base64", json.dumps(events).lower())
        columns = {row[1] for row in service.store._db.execute("PRAGMA table_info(jobs)")}
        self.assertNotIn("output_url", columns)
        row_values = service.store._db.execute("SELECT * FROM jobs").fetchone()
        self.assertNotIn(FAKE_SIGNED_QUERY, repr(tuple(row_values)))

        # A repeated logical message after a worker reconstruction returns its
        # existing completed record and cannot create another provider job.
        old_store = service.store
        old_store.close()
        self.stores.remove(old_store)
        restarted_store = JobStore(old_store.path)
        self.stores.append(restarted_store)
        recovered_backend = backend
        recovered_persistence = persistence
        restarted_service = WorkerService(
            restarted_store, recovered_backend, recovered_persistence,
            SecretBox(b"e" * 32), b"f" * 32,
            artifact_directory=self.downloaders[0].artifact_directory,
            downloader=self.downloaders[0], saved_chat_verifier=verifier,
        )
        restarted_app = create_app(restarted_service, FAKE_WORKER_BEARER)
        restarted_pipe, _, _ = self.make_pipe(restarted_app, restarted_service)
        restart_events = []

        async def emit_restart(event):
            restart_events.append(event)

        restarted_result = await restarted_pipe.pipe(
            self.body(), __user__=context["user"], __metadata__=context["metadata"],
            __message_id__=context["message_id"], __request__=context["request"],
            __event_emitter__=emit_restart,
        )
        self.assertIn("completed", restarted_result)
        self.assertEqual(backend.submit_calls, 1)
        self.assertEqual(persistence.calls, 1)

    async def test_conflicting_payload_for_same_message_is_rejected(self):
        service, backend, _, _, app, _ = self.make_worker()
        pipe, _, _ = self.make_pipe(app, service, advance_on_get=False)
        context = self.request_context()
        await pipe.pipe(self.body("first intent"), __user__=context["user"],
                        __metadata__=context["metadata"], __message_id__=MESSAGE,
                        __request__=context["request"], __event_emitter__=_event_sink)
        with self.assertRaisesRegex(WorkerClientError, "VIDEO_MESSAGE_CONFLICT"):
            await pipe.pipe(self.body("conflicting paid intent"), __user__=context["user"],
                            __metadata__=context["metadata"], __message_id__=MESSAGE,
                            __request__=context["request"], __event_emitter__=_event_sink)
        self.assertEqual(backend.submit_calls, 1)

    async def test_unsaved_message_verifier_blocks_provider_submission(self):
        service, backend, _, verifier, app, _ = self.make_worker(saved_messages=set())
        pipe, _, _ = self.make_pipe(app, service)
        context = self.request_context()
        await pipe.pipe(self.body(), __user__=context["user"], __metadata__=context["metadata"],
                        __message_id__=MESSAGE, __request__=context["request"],
                        __event_emitter__=_event_sink)
        rows = service.store._db.execute("SELECT worker_state,last_safe_error FROM jobs").fetchall()
        self.assertEqual(tuple(rows[0]), ("failed", "SAVED_CHAT_UNAVAILABLE"))
        submit_attempts = service.store._db.execute("SELECT submit_attempts FROM jobs").fetchone()[0]
        self.assertEqual(submit_attempts, 0)
        self.assertEqual(backend.submit_calls, 0)
        self.assertEqual(verifier.calls, [(OWNER, CHAT, MESSAGE)])

    async def test_duplicate_equivalent_request_reuses_job_without_second_create(self):
        service, backend, _, _, app, _ = self.make_worker()
        pipe, _, transports = self.make_pipe(app, service, advance_on_get=False)
        context = self.request_context()
        for _ in range(2):
            await pipe.pipe(self.body(), __user__=context["user"], __metadata__=context["metadata"],
                            __message_id__=MESSAGE, __request__=context["request"],
                            __event_emitter__=_event_sink)
        creates = [entry for transport in transports for entry in transport.requests
                   if entry[0] == "POST" and entry[1] == "/jobs"]
        self.assertEqual(len(creates), 2)  # one logical request per invocation
        self.assertEqual(backend.submit_calls, 1)
        jobs = service.store._db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        self.assertEqual(jobs, 1)

    async def test_ambiguous_submit_is_not_repeated_on_equivalent_invocation(self):
        service, backend, _, _, app, _ = self.make_worker(submit_error=TimeoutError())
        pipe, _, _ = self.make_pipe(app, service, advance_on_get=False)
        context = self.request_context()
        for _ in range(2):
            await pipe.pipe(self.body(), __user__=context["user"], __metadata__=context["metadata"],
                            __message_id__=MESSAGE, __request__=context["request"],
                            __event_emitter__=_event_sink)
        row = service.store._db.execute("SELECT worker_state,submit_attempts FROM jobs").fetchone()
        self.assertEqual(tuple(row), ("submit_ambiguous", 1))
        self.assertEqual(backend.submit_calls, 1)

    async def test_cross_user_status_cancel_and_refresh_are_denied(self):
        service, _, _, _, app, _ = self.make_worker()
        pipe, _, _ = self.make_pipe(app, service, advance_on_get=False)
        context = self.request_context()
        await pipe.pipe(self.body(), __user__=context["user"], __metadata__=context["metadata"],
                        __message_id__=MESSAGE, __request__=context["request"],
                        __event_emitter__=_event_sink)
        job_id = service.store._db.execute("SELECT job_id FROM jobs").fetchone()[0]
        other = self.request_context(user_id="user-2")
        for action in ("status", "cancel"):
            with self.assertRaisesRegex(WorkerClientError, "VIDEO_JOB_NOT_FOUND"):
                await pipe.pipe({"video_action": action, "video_job_id": job_id},
                                __user__=other["user"], __request__=other["request"],
                                __event_emitter__=_event_sink)

        # The refresh route has the same owner check even when called directly.
        client = VideoWorkerClient(FakeBearerProvider(), transport=WorkerASGITransport(app, service))
        try:
            with self.assertRaisesRegex(WorkerClientError, "VIDEO_JOB_NOT_FOUND"):
                await client.refresh_credential(owner_user_id="user-2", job_id=job_id,
                                                user_credential="another-fake-user-jwt")
        finally:
            await client.aclose()
        record = service.store.get(job_id)
        self.assertFalse(record.cancel_requested)

    async def test_owner_can_request_cancel_without_implying_provider_refund(self):
        service, backend, _, _, app, _ = self.make_worker()
        pipe, _, _ = self.make_pipe(app, service, advance_on_get=False)
        context = self.request_context()
        await pipe.pipe(self.body(), __user__=context["user"], __metadata__=context["metadata"],
                        __message_id__=MESSAGE, __request__=context["request"],
                        __event_emitter__=_event_sink)
        job_id = service.store._db.execute("SELECT job_id FROM jobs").fetchone()[0]
        await pipe.pipe({"video_action": "cancel", "video_job_id": job_id},
                        __user__=context["user"], __event_emitter__=_event_sink)
        self.assertTrue(service.store.get(job_id).cancel_requested)
        self.assertEqual(backend.cancel_calls, 0)  # provider cancellation is worker-scheduled

    async def test_transient_poll_failure_recovers_known_id_without_resubmit(self):
        service, backend, _, _, app, _ = self.make_worker()
        backend.set_results(
            "video_1", TimeoutError(),
            UpstreamJob("video_1", "in_progress"),
        )
        pipe, _, _ = self.make_pipe(app, service)
        context = self.request_context()
        result = await pipe.pipe(self.body(), __user__=context["user"],
                                 __metadata__=context["metadata"], __message_id__=MESSAGE,
                                 __request__=context["request"], __event_emitter__=_event_sink)
        job = service.store.record(service.store._db.execute("SELECT * FROM jobs").fetchone())
        self.assertIn("video job", result.lower())
        self.assertEqual(job.upstream_job_id, "video_1")
        self.assertEqual(backend.submit_calls, 1)

    async def test_provider_failure_returns_safe_terminal_status(self):
        service, backend, _, _, app, _ = self.make_worker()
        backend.set_results("video_1", UpstreamJob("video_1", "failed", error_code="MOCK_PROVIDER_FAILED"))
        pipe, _, _ = self.make_pipe(app, service)
        context = self.request_context()
        result = await pipe.pipe(self.body(), __user__=context["user"],
                                 __metadata__=context["metadata"], __message_id__=MESSAGE,
                                 __request__=context["request"], __event_emitter__=_event_sink)
        self.assertIn("MOCK_PROVIDER_FAILED", result)
        self.assertEqual(backend.submit_calls, 1)

    async def test_result_download_failure_is_durable_and_never_exposes_url(self):
        service, backend, _, _, app, _ = self.make_worker(download_body=b"<html>error</html>")
        url = f"https://result.invalid/video.mp4?signature={FAKE_SIGNED_QUERY}"
        backend.set_results(
            "video_1", UpstreamJob("video_1", "completed", output_mime="video/mp4",
                                   output_url=url, expected_bytes=24),
        )
        pipe, _, _ = self.make_pipe(app, service)
        context = self.request_context()
        result = await pipe.pipe(self.body(), __user__=context["user"],
                                 __metadata__=context["metadata"], __message_id__=MESSAGE,
                                 __request__=context["request"], __event_emitter__=_event_sink)
        self.assertIn("RESULT_DOWNLOAD_REJECTED", result)
        self.assertNotIn(FAKE_SIGNED_QUERY, result)
        row = service.store._db.execute("SELECT worker_state,last_safe_error FROM jobs").fetchone()
        self.assertEqual(tuple(row), ("failed", "RESULT_DOWNLOAD_REJECTED"))
        self.assertFalse(list(self.downloaders[0].artifact_directory.glob("*.part")))

    async def test_delivery_auth_required_refresh_reuses_same_result(self):
        persistence = FakePersistence(fail_auth=True)
        service, backend, _, _, app, _ = self.make_worker(persistence=persistence)
        backend.set_results(
            "video_1", UpstreamJob("video_1", "completed", output_mime="video/mp4",
                                   output_bytes=MP4),
        )
        pipe, _, _ = self.make_pipe(app, service)
        context = self.request_context()
        first = await pipe.pipe(self.body(), __user__=context["user"],
                                __metadata__=context["metadata"], __message_id__=MESSAGE,
                                __request__=context["request"], __event_emitter__=_event_sink)
        self.assertIn("refresh Open WebUI authentication", first)
        job_id = service.store._db.execute("SELECT job_id FROM jobs").fetchone()[0]
        persistence.fail_auth = False
        status_context = self.request_context()
        second = await pipe.pipe(
            {"video_action": "status", "video_job_id": job_id},
            __user__=status_context["user"], __request__=status_context["request"],
            __event_emitter__=_event_sink,
        )
        self.assertEqual(service.store.get(job_id).worker_state, WorkerState.PERSISTING)
        self.assertEqual(backend.submit_calls, 1)
        self.assertIn("persisting", second)

    async def test_bounded_pipe_wait_returns_recoverable_status_without_cancel(self):
        service, backend, _, _, app, _ = self.make_worker()
        pipe, _, _ = self.make_pipe(app, service, advance_on_get=False)
        pipe.valves.MAX_STATUS_CHECKS = 1
        context = self.request_context()
        result = await pipe.pipe(self.body(), __user__=context["user"],
                                 __metadata__=context["metadata"], __message_id__=MESSAGE,
                                 __request__=context["request"], __event_emitter__=_event_sink)
        job = service.store._db.execute("SELECT * FROM jobs").fetchone()
        self.assertIn("Check this message again", result)
        self.assertEqual(job["worker_state"], "queued")
        self.assertFalse(job["cancel_requested"])
        self.assertEqual(backend.submit_calls, 1)

    async def test_cookie_normalization_passes_only_the_token_value(self):
        credential = current_user_credential(TestRequest(cookies={
            "token": FAKE_USER_JWT, "theme": "private-cookie-setting",
        }))
        self.assertEqual(credential, FAKE_USER_JWT)
        with self.assertRaisesRegex(VideoPipeError, "ambiguous"):
            current_user_credential(TestRequest(
                bearer=FAKE_USER_JWT, cookies={"token": "different-fake-jwt"},
            ))

    async def test_http_debug_bearer_values_are_redacted(self):
        record = logging.LogRecord(
            "httpcore", logging.DEBUG, "client.py", 1,
            "Sending Authorization: Bearer fake-worker-only-bearer-for-test", (), None,
        )
        for filter_ in logging.getLogger("httpcore").filters:
            filter_.filter(record)
        self.assertNotIn(FAKE_WORKER_BEARER, record.getMessage())
        self.assertIn("[REDACTED]", record.getMessage())

    async def test_worker_response_is_bounded_and_errors_do_not_leak_bearer(self):
        for response in (
            httpx.Response(200, json={"state": "future_state", "job_id": "job-1"}),
            httpx.Response(200, json={"state": [], "job_id": "job-1"}),
            httpx.Response(200, json={"state": "queued", "job_id": "job-1", "upstream_state": []}),
            httpx.Response(200, content=b"{}", headers={"content-type": "text/html"}),
            httpx.Response(200, content=b"x" * (MAX_WORKER_RESPONSE_BYTES + 1)),
        ):
            client = VideoWorkerClient(
                FakeBearerProvider(), transport=httpx.MockTransport(lambda _request, r=response: r),
            )
            try:
                with self.assertRaises(WorkerClientError) as caught:
                    await client.get_job(owner_user_id=OWNER, job_id="job-1")
            finally:
                await client.aclose()
            self.assertNotIn(FAKE_WORKER_BEARER, str(caught.exception))

    async def test_malformed_worker_response_is_sanitized(self):
        client = VideoWorkerClient(
            FakeBearerProvider(),
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"not-json")),
        )
        try:
            with self.assertRaisesRegex(WorkerClientError, "WORKER_RESPONSE_INVALID"):
                await client.get_job(owner_user_id=OWNER, job_id="job-1")
        finally:
            await client.aclose()


async def _no_sleep(_delay: float) -> None:
    return None


async def _event_sink(_event: dict) -> None:
    return None


if __name__ == "__main__":
    unittest.main()
