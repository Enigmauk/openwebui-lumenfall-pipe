"""No-network tests for the real Lumenfall HTTP and result-download collaborators."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

import httpx

from video_worker.downloader import (
    MAX_DOWNLOAD_BYTES, DownloadError, SecureResultDownloader,
)
from video_worker.lumenfall_http import LumenfallVideoClient
from video_worker.db import JobStore
from video_worker.integrations import create_http_worker_service
from video_worker.fakes import FakeSavedChatVerifier
from video_worker.models import AmbiguousSubmit, PermanentVideoError, WorkerState
from video_worker.security import SecretBox


FAKE_KEY = "fake-lumenfall-key-for-tests-only"
SIGNED_SECRET = "fake-signed-output-query-for-tests-only"
PUBLIC_IP = "8.8.8.8"


class FakeKeyProvider:
    def get_key(self) -> str:
        return FAKE_KEY


def queued_payload(job_id="video_test_1"):
    return {
        "id": job_id,
        "object": "video",
        "status": "queued",
        "metadata": {"provider": "fal", "executed_model": "vendor/model-v2"},
    }


def completed_payload(job_id="video_test_1", *, url=None, mime="video/mp4", size=24):
    return {
        "id": job_id,
        "object": "video",
        "status": "completed",
        "output": {
            "url": url or f"https://cdn.example/result.mp4?signature={SIGNED_SECRET}",
            "content_type": mime,
            "size_bytes": size,
        },
        "metadata": {
            "provider": "fal",
            "executed_model": "vendor/model-v2",
            "cost": 0.125,
            "cost_currency": "USD",
        },
    }


class LumenfallHTTPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_202_sends_stable_idempotency_and_exactly_one_post(self):
        calls = []

        def handle(request):
            calls.append(request)
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "https://api.lumenfall.ai/openai/v1/videos")
            self.assertEqual(request.headers["authorization"], f"Bearer {FAKE_KEY}")
            body = json.loads(request.content)
            self.assertEqual(body["idempotency_key"], "persisted-key-123")
            self.assertEqual(body["model"], "vendor/model-v2")
            self.assertEqual(body["prompt"], "fake test prompt")
            self.assertEqual(body["seconds"], 5)
            return httpx.Response(202, json=queued_payload())

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(handle))
        try:
            result = await client.submit(
                model_id="vendor/model-v2", prompt="fake test prompt",
                options={"seconds": 5}, idempotency_key="persisted-key-123",
            )
        finally:
            await client.aclose()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.job_id, "video_test_1")
        self.assertEqual(result.state, "queued")

    async def test_create_timeout_is_ambiguous_and_not_retried(self):
        calls = 0

        def handle(_request):
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("contains no useful public detail")

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(AmbiguousSubmit) as caught:
                await client.submit(
                    model_id="vendor/model-v2", prompt="fake prompt", options={},
                    idempotency_key="stable-test-key",
                )
        finally:
            await client.aclose()
        self.assertEqual(calls, 1)
        self.assertEqual(str(caught.exception), "LUMENFALL_CREATE_OUTCOME_UNKNOWN")

    async def test_malformed_or_missing_create_id_is_ambiguous(self):
        for payload in ({"status": "queued"}, {"id": "", "status": "queued"}, b"{"):
            with self.subTest(payload=payload):
                calls = 0

                def handle(_request):
                    nonlocal calls
                    calls += 1
                    if isinstance(payload, bytes):
                        return httpx.Response(202, content=payload)
                    return httpx.Response(202, json=payload)

                client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(handle))
                try:
                    with self.assertRaises(AmbiguousSubmit):
                        await client.submit(
                            model_id="vendor/model-v2", prompt="fake prompt", options={},
                            idempotency_key="stable-test-key",
                        )
                finally:
                    await client.aclose()
                self.assertEqual(calls, 1)

    async def test_unexpected_success_status_is_ambiguous_without_retry(self):
        calls = 0

        def handle(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(201, json=queued_payload())

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(AmbiguousSubmit):
                await client.submit(
                    model_id="vendor/model-v2", prompt="fake prompt", options={},
                    idempotency_key="stable-test-key",
                )
        finally:
            await client.aclose()
        self.assertEqual(calls, 1)

    async def test_queued_in_progress_completed_and_cost_metadata(self):
        payloads = [
            queued_payload(),
            {**queued_payload(), "status": "in_progress"},
            completed_payload(),
        ]

        def handle(_request):
            return httpx.Response(200, json=payloads.pop(0))

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(handle))
        try:
            queued = await client.get("video_test_1")
            progress = await client.get("video_test_1")
            completed = await client.get("video_test_1")
        finally:
            await client.aclose()
        self.assertEqual([queued.state, progress.state, completed.state],
                         ["queued", "in_progress", "completed"])
        self.assertEqual(completed.provider, "fal")
        self.assertEqual(completed.executed_model, "vendor/model-v2")
        self.assertEqual(completed.cost_micros, 125_000)
        self.assertEqual(completed.cost_currency, "USD")
        self.assertEqual(completed.expected_bytes, 24)
        self.assertEqual(completed.output_mime, "video/mp4")
        self.assertIn(SIGNED_SECRET, completed.output_url)

    async def test_provider_failure_is_sanitized(self):
        payload = {"id": "video_test_1", "status": "failed",
                   "error": {"code": "provider_busy", "message": "do not retain this detail"}}
        client = LumenfallVideoClient(
            FakeKeyProvider(), transport=httpx.MockTransport(lambda _req: httpx.Response(200, json=payload))
        )
        try:
            result = await client.get("video_test_1")
        finally:
            await client.aclose()
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "provider_busy")

    async def test_unknown_state_and_invalid_result_metadata_fail_closed(self):
        payloads = [
            {"id": "video_test_1", "status": "running"},
            {"id": "video_test_1", "status": "completed", "output": {
                "url": "http://cdn.example/video.mp4", "content_type": "video/mp4", "size_bytes": 12,
            }},
            {"id": "video_test_1", "status": "completed", "output": {
                "url": "https://cdn.example/video.mp4", "content_type": "video/mp4", "size_bytes": True,
            }},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                client = LumenfallVideoClient(
                    FakeKeyProvider(),
                    transport=httpx.MockTransport(lambda _req, item=payload: httpx.Response(200, json=item)),
                )
                try:
                    with self.assertRaises(PermanentVideoError):
                        await client.get("video_test_1")
                finally:
                    await client.aclose()

    async def test_get_retries_transient_failures_with_a_bound(self):
        calls = 0
        delays = []

        async def no_sleep(delay):
            delays.append(delay)

        def handle(_request):
            nonlocal calls
            calls += 1
            return httpx.Response(503) if calls < 3 else httpx.Response(200, json=queued_payload())

        client = LumenfallVideoClient(
            FakeKeyProvider(), transport=httpx.MockTransport(handle), sleep=no_sleep
        )
        try:
            job = await client.get("video_test_1")
        finally:
            await client.aclose()
        self.assertEqual(job.state, "queued")
        self.assertEqual(calls, 3)
        self.assertEqual(delays, [0.1, 0.2])

    async def test_malformed_json_and_oversized_get_response_are_rejected(self):
        for response in (
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, content=b"{}", headers={"content-length": "600000"}),
        ):
            client = LumenfallVideoClient(
                FakeKeyProvider(), transport=httpx.MockTransport(lambda _req, r=response: r)
            )
            try:
                with self.assertRaises(PermanentVideoError):
                    await client.get("video_test_1")
            finally:
                await client.aclose()

    async def test_cancel_success_and_failure_have_sanitized_errors(self):
        calls = []

        def success(request):
            calls.append(request.method)
            return httpx.Response(204)

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(success))
        try:
            await client.cancel("video_test_1")
        finally:
            await client.aclose()
        self.assertEqual(calls, ["DELETE"])

        def failure(request):
            calls.append(request.method)
            return httpx.Response(500, content=SIGNED_SECRET.encode())

        client = LumenfallVideoClient(FakeKeyProvider(), transport=httpx.MockTransport(failure))
        try:
            with self.assertRaises(PermanentVideoError) as caught:
                await client.cancel("video_test_1")
        finally:
            await client.aclose()
        self.assertEqual(calls[-1], "DELETE")
        self.assertNotIn(SIGNED_SECRET, str(caught.exception))

    async def test_create_errors_never_expose_fake_bearer(self):
        client = LumenfallVideoClient(
            FakeKeyProvider(), transport=httpx.MockTransport(lambda _req: httpx.Response(500, content=FAKE_KEY))
        )
        try:
            with self.assertRaises(AmbiguousSubmit) as caught:
                await client.submit(model_id="vendor/model-v2", prompt="private prompt", options={},
                                    idempotency_key="stable-test-key")
        finally:
            await client.aclose()
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        self.assertNotIn("private prompt", str(caught.exception))

    async def test_base_url_cannot_be_overridden(self):
        with self.assertRaises(TypeError):
            LumenfallVideoClient(FakeKeyProvider(), base_url="https://attacker.example")


def mp4_bytes(size=24):
    return (b"\x00\x00\x00\x10ftypmp42\x00\x00\x00\x00" + b"x" * max(0, size - 16))


WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x93\x42\x86\x81\x01"


class RepeatingStream(httpx.AsyncByteStream):
    def __init__(self, total: int, prefix: bytes):
        self.total = total
        self.prefix = prefix

    async def __aiter__(self):
        emitted = 0
        first = self.prefix[:min(len(self.prefix), self.total)]
        if first:
            emitted += len(first)
            yield first
        block = b"x" * (64 * 1024)
        while emitted < self.total:
            chunk = block[:min(len(block), self.total - emitted)]
            emitted += len(chunk)
            yield chunk

    async def aclose(self):
        return None


class SecureDownloaderTests(unittest.IsolatedAsyncioTestCase):
    def downloader(self, directory, handler, *, resolver=None, max_bytes=MAX_DOWNLOAD_BYTES,
                   redirects=3):
        return SecureResultDownloader(
            directory,
            transport=httpx.MockTransport(handler),
            resolver=resolver or (lambda _host, _port: [PUBLIC_IP]),
            max_bytes=max_bytes,
            maximum_redirects=redirects,
        )

    async def test_valid_mp4_streams_to_private_final_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "artifacts"
            client = self.downloader(
                root,
                lambda req: httpx.Response(200, headers={"content-type": "video/mp4"}, content=mp4_bytes()),
            )
            try:
                result = await client.download(
                    job_id=str(uuid.uuid4()), url=f"https://cdn.example/a.mp4?sig={SIGNED_SECRET}",
                    mime="video/mp4", expected_bytes=24,
                )
            finally:
                await client.aclose()
            final = root / result.artifact_path
            self.assertEqual(final.read_bytes(), mp4_bytes())
            self.assertEqual(result.actual_bytes, 24)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o600)
            self.assertFalse((root / f".{final.stem}.part").exists())

    async def test_valid_webm_streams_to_final_artifact(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(
                temp,
                lambda _req: httpx.Response(200, headers={"content-type": "video/webm"}, content=WEBM_BYTES),
            )
            try:
                result = await client.download(
                    job_id=str(uuid.uuid4()), url="https://cdn.example/a.webm", mime="video/webm",
                    expected_bytes=len(WEBM_BYTES),
                )
            finally:
                await client.aclose()
            self.assertTrue((Path(temp) / result.artifact_path).read_bytes().startswith(b"\x1a\x45\xdf\xa3"))

    async def test_https_and_unsafe_ip_literals_are_rejected_before_request(self):
        unsafe = [
            "http://cdn.example/file.mp4",
            "https://127.0.0.1/file.mp4",
            "https://10.20.30.40/file.mp4",
            "https://169.254.2.3/file.mp4",
            "https://[::1]/file.mp4",
            "https://[fe80::1]/file.mp4",
            "https://[fd00::1]/file.mp4",
            "https://user:pass@cdn.example/file.mp4",
        ]
        for url in unsafe:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temp:
                calls = 0

                def handle(_req):
                    nonlocal calls
                    calls += 1
                    return httpx.Response(200, content=mp4_bytes(), headers={"content-type": "video/mp4"})

                client = self.downloader(temp, handle)
                try:
                    with self.assertRaises(DownloadError):
                        await client.download(job_id=str(uuid.uuid4()), url=url, mime="video/mp4")
                finally:
                    await client.aclose()
                self.assertEqual(calls, 0)

    async def test_dns_failure_and_mixed_public_private_answers_fail_closed(self):
        for resolver in (
            lambda _host, _port: (_ for _ in ()).throw(socket.gaierror("private detail")),
            lambda _host, _port: [PUBLIC_IP, "192.168.1.20"],
        ):
            with tempfile.TemporaryDirectory() as temp:
                client = self.downloader(
                    temp, lambda _req: httpx.Response(200), resolver=resolver
                )
                try:
                    with self.assertRaises(DownloadError):
                        await client.download(job_id=str(uuid.uuid4()), url="https://cdn.example/x.mp4",
                                              mime="video/mp4")
                finally:
                    await client.aclose()

    async def test_redirect_to_public_host_revalidates_each_target(self):
        validated = []
        calls = []

        def resolver(host, _port):
            validated.append(host)
            return [PUBLIC_IP]

        def handle(request):
            calls.append(str(request.url))
            if request.url.host == "origin.example":
                return httpx.Response(302, headers={"location": f"https://cdn.example/out.mp4?sig={SIGNED_SECRET}"})
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=mp4_bytes())

        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(temp, handle, resolver=resolver)
            try:
                result = await client.download(job_id=str(uuid.uuid4()),
                                               url="https://origin.example/start", mime="video/mp4")
            finally:
                await client.aclose()
        self.assertEqual(validated, ["origin.example", "cdn.example"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.actual_bytes, 24)

    async def test_redirect_to_private_target_is_rejected(self):
        calls = []

        def handle(request):
            calls.append(request.url.host)
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private.mp4"})

        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(temp, handle)
            try:
                with self.assertRaises(DownloadError):
                    await client.download(job_id=str(uuid.uuid4()), url="https://origin.example/start",
                                          mime="video/mp4")
            finally:
                await client.aclose()
        self.assertEqual(calls, ["origin.example"])

    async def test_redirect_limit_is_enforced(self):
        calls = []

        def handle(request):
            calls.append(request.url.host)
            return httpx.Response(302, headers={"location": f"https://host{len(calls)}.example/next"})

        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(temp, handle, redirects=2)
            try:
                with self.assertRaises(DownloadError):
                    await client.download(job_id=str(uuid.uuid4()), url="https://origin.example/start",
                                          mime="video/mp4")
            finally:
                await client.aclose()
        self.assertEqual(len(calls), 3)

    async def test_content_length_over_256_mib_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = 0

            def handle(_req):
                nonlocal calls
                calls += 1
                return httpx.Response(200, headers={
                    "content-type": "video/mp4",
                    "content-length": str(MAX_DOWNLOAD_BYTES + 1),
                }, content=b"")

            client = self.downloader(temp, handle)
            try:
                with self.assertRaises(DownloadError):
                    await client.download(job_id=str(uuid.uuid4()), url="https://cdn.example/a.mp4",
                                          mime="video/mp4")
            finally:
                await client.aclose()
            self.assertEqual(calls, 1)
            self.assertEqual(list(Path(temp).iterdir()), [])

    async def test_streamed_body_crossing_256_mib_is_aborted_and_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            def handle(_req):
                return httpx.Response(
                    200,
                    headers={"content-type": "video/mp4"},
                    stream=RepeatingStream(MAX_DOWNLOAD_BYTES + 1, mp4_bytes()),
                )

            client = self.downloader(temp, handle)
            job_id = str(uuid.uuid4())
            try:
                with self.assertRaises(DownloadError):
                    await client.download(job_id=job_id, url="https://cdn.example/a.mp4", mime="video/mp4")
            finally:
                await client.aclose()
            self.assertEqual(list(Path(temp).iterdir()), [])

    async def test_mime_mismatch_and_html_or_error_body_are_rejected(self):
        responses = [
            httpx.Response(200, headers={"content-type": "video/webm"}, content=mp4_bytes()),
            httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>error</html>"),
            httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"<html>error</html>"),
        ]
        for response in responses:
            with tempfile.TemporaryDirectory() as temp:
                client = self.downloader(temp, lambda _req, r=response: r)
                try:
                    with self.assertRaises(DownloadError):
                        await client.download(job_id=str(uuid.uuid4()), url="https://cdn.example/a.mp4",
                                              mime="video/mp4")
                finally:
                    await client.aclose()
                self.assertEqual(list(Path(temp).iterdir()), [])

    async def test_bad_mp4_and_webm_signatures_are_rejected(self):
        for mime, body in (("video/mp4", b"not an mp4 container"),
                           ("video/webm", b"not an ebml container")):
            with tempfile.TemporaryDirectory() as temp:
                client = self.downloader(
                    temp,
                    lambda _req, content=body, content_type=mime: httpx.Response(
                        200, headers={"content-type": content_type}, content=content
                    ),
                )
                try:
                    with self.assertRaises(DownloadError):
                        await client.download(job_id=str(uuid.uuid4()), url="https://cdn.example/file",
                                              mime=mime)
                finally:
                    await client.aclose()
                self.assertEqual(list(Path(temp).iterdir()), [])

    async def test_size_mismatch_cleans_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(
                temp,
                lambda _req: httpx.Response(200, headers={"content-type": "video/mp4"}, content=mp4_bytes()),
            )
            job_id = str(uuid.uuid4())
            try:
                with self.assertRaises(DownloadError):
                    await client.download(job_id=job_id, url="https://cdn.example/a.mp4",
                                          mime="video/mp4", expected_bytes=25)
            finally:
                await client.aclose()
            self.assertEqual(list(Path(temp).iterdir()), [])

    async def test_signed_url_is_never_in_download_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            client = self.downloader(
                temp, lambda _req: httpx.Response(403, content=SIGNED_SECRET.encode())
            )
            try:
                with self.assertLogs("httpx", level="INFO") as logs:
                    with self.assertRaises(DownloadError) as caught:
                        await client.download(
                            job_id=str(uuid.uuid4()),
                            url=f"https://cdn.example/a.mp4?sig={SIGNED_SECRET}", mime="video/mp4",
                        )
            finally:
                await client.aclose()
            self.assertNotIn(SIGNED_SECRET, "\n".join(logs.output))
            self.assertNotIn("cdn.example", "\n".join(logs.output))
            self.assertNotIn(SIGNED_SECRET, str(caught.exception))
            self.assertNotIn("cdn.example", str(caught.exception))


class RealCollaboratorWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_worker_downloads_and_persists_without_storing_signed_url(self):
        media = mp4_bytes()
        api_calls = []
        download_calls = []

        def api(request):
            api_calls.append(request)
            if request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["idempotency_key"], test_job.idempotency_key)
                self.assertEqual(request.headers["authorization"], f"Bearer {FAKE_KEY}")
                return httpx.Response(202, json=queued_payload())
            if request.method == "DELETE":
                return httpx.Response(503, content=SIGNED_SECRET.encode())
            return httpx.Response(200, json=completed_payload(size=len(media)))

        def download(request):
            download_calls.append(request)
            return httpx.Response(200, headers={"content-type": "video/mp4"}, content=media)

        class Persistence:
            def __init__(self):
                self.content = None

            async def persist(self, *, job_id, content, mime, credential,
                              owner_user_id, chat_id, assistant_message_id):
                self.content = content
                self.asserted = (job_id, mime, credential, owner_user_id, chat_id,
                                 assistant_message_id)
                return f"file_{job_id}"

        with tempfile.TemporaryDirectory() as temp:
            store = JobStore(Path(temp) / "worker.sqlite3")
            persistence = Persistence()
            artifacts = Path(temp) / "private-artifacts"
            service = create_http_worker_service(
                store=store,
                persistence=persistence,
                secret_box=SecretBox(b"e" * 32),
                fingerprint_key=b"f" * 32,
                key_provider=FakeKeyProvider(),
                saved_chat_verifier=FakeSavedChatVerifier(),
                artifact_directory=artifacts,
                api_transport=httpx.MockTransport(api),
                download_transport=httpx.MockTransport(download),
                resolver=lambda host, _port: [PUBLIC_IP],
            )
            try:
                test_job, _ = service.create_job(
                    owner_user_id="user-test", chat_id="chat-test",
                    assistant_message_id="message-test", friendly_model="Mock Video",
                    model_id="vendor/model-v2", prompt="fake integration prompt",
                    options={"seconds": 5}, user_credential="fake-openwebui-credential",
                )
                queued = await service.advance(test_job.job_id)
                self.assertEqual(queued.worker_state, WorkerState.QUEUED)
                await service.request_cancel(test_job.job_id, "user-test")
                downloaded = await service.advance(test_job.job_id)
                self.assertEqual(downloaded.worker_state, WorkerState.DOWNLOADING)
                self.assertEqual(downloaded.upstream_job_id, "video_test_1")
                self.assertEqual(downloaded.upstream_state, "completed")
                self.assertEqual(downloaded.downloaded_bytes, len(media))
                self.assertEqual(downloaded.expected_bytes, len(media))
                self.assertEqual(downloaded.final_cost_micros, 125_000)
                self.assertEqual(downloaded.artifact_path, f"{test_job.job_id}.mp4")
                self.assertTrue((artifacts / downloaded.artifact_path).is_file())
                self.assertNotIn(SIGNED_SECRET, repr(downloaded))
                columns = {row[1] for row in store._db.execute("PRAGMA table_info(jobs)")}
                self.assertNotIn("output_url", columns)

                final = await service.run_until_stable(test_job.job_id)
                self.assertEqual(final.worker_state, WorkerState.COMPLETED)
                self.assertEqual(final.openwebui_file_id, f"file_{test_job.job_id}")
                self.assertEqual(persistence.content, media)
                self.assertEqual(persistence.asserted,
                                 (test_job.job_id, "video/mp4", "fake-openwebui-credential",
                                  "user-test", "chat-test", "message-test"))
            finally:
                await service.backend.aclose()
                await service.downloader.aclose()
                store.close()
        self.assertEqual([request.method for request in api_calls], ["POST", "DELETE", "GET"])
        self.assertEqual(len(download_calls), 1)
        self.assertEqual(api_calls[0].headers["authorization"], f"Bearer {FAKE_KEY}")
        self.assertNotIn("fake-openwebui-credential", api_calls[0].headers["authorization"])


if __name__ == "__main__":
    unittest.main()
