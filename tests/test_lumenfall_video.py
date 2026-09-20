import unittest

from lumenfall_video import (
    AmbiguousSubmission,
    PollPolicy,
    VideoError,
    VideoJob,
    VideoLifecycle,
    VideoOutput,
    VideoPrototypePipe,
    VideoStatus,
    parse_video_job,
    validate_remote_video_url,
    validate_video_size,
    video_selector_entries,
)


class FakeClient:
    def __init__(self, results=None, submit_error=None):
        self.results = list(results or [])
        self.submit_error = submit_error
        self.submit_calls = 0
        self.get_calls = 0

    async def submit(self, **kwargs):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        return VideoJob("video_1", VideoStatus.QUEUED)

    async def get(self, job_id):
        self.get_calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def cancel(self, job_id):
        return None


async def no_sleep(_):
    return None


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_internal_task_rejected_before_submit(self):
        client = FakeClient()
        result = await VideoPrototypePipe(VideoLifecycle(client, no_sleep)).pipe(
            task="title_generation", model="p-video", prompt="x", idempotency_key="k"
        )
        self.assertEqual(result, "")
        self.assertEqual(client.submit_calls, 0)

    async def test_submit_exactly_once(self):
        client = FakeClient()
        job = await VideoLifecycle(client, no_sleep).start_once(model="p-video", prompt="x", idempotency_key="k")
        self.assertEqual(job.job_id, "video_1")
        self.assertEqual(client.submit_calls, 1)

    async def test_ambiguous_submit_is_not_retried(self):
        client = FakeClient(submit_error=TimeoutError())
        with self.assertRaises(AmbiguousSubmission):
            await VideoLifecycle(client, no_sleep).start_once(model="p-video", prompt="x", idempotency_key="k")
        self.assertEqual(client.submit_calls, 1)

    async def test_queued_processing_completed(self):
        client = FakeClient([VideoJob("v", VideoStatus.QUEUED), VideoJob("v", VideoStatus.IN_PROGRESS), VideoJob("v", VideoStatus.COMPLETED, VideoOutput("https://media.example/v.mp4", "video/mp4", 10))])
        job = await VideoLifecycle(client, no_sleep).poll("v", PollPolicy(0, 0, 0, 2, 2))
        self.assertEqual(job.status, VideoStatus.COMPLETED)

    async def test_safe_polling_retry(self):
        client = FakeClient([TimeoutError(), VideoJob("v", VideoStatus.COMPLETED, VideoOutput("https://media.example/v.mp4", "video/mp4"))])
        job = await VideoLifecycle(client, no_sleep).poll("v", PollPolicy(0, 0, 0, 2, 2))
        self.assertEqual(job.status, VideoStatus.COMPLETED)

    async def test_terminal_failure(self):
        client = FakeClient([VideoJob("v", VideoStatus.FAILED, error_code="UPSTREAM")])
        with self.assertRaises(VideoError):
            await VideoLifecycle(client, no_sleep).poll("v", PollPolicy(0, 0, 0, 1, 0))

    async def test_timeout_preserves_job_id(self):
        client = FakeClient([VideoJob("v", VideoStatus.QUEUED)] * 3)
        with self.assertRaisesRegex(VideoError, "known video job is v"):
            await VideoLifecycle(client, no_sleep).poll("v", PollPolicy(0, 1, 1, 1, 0))


class ParsingAndSecurityTests(unittest.TestCase):
    def test_missing_job_id(self):
        with self.assertRaises(VideoError): parse_video_job({"status": "queued"})

    def test_unknown_status(self):
        with self.assertRaises(VideoError): parse_video_job({"id": "v", "status": "cancelled"})

    def test_invalid_video_mime(self):
        with self.assertRaises(VideoError): parse_video_job({"id": "v", "status": "completed", "output": {"url": "https://x/v", "content_type": "text/html"}})

    def test_oversized_output(self):
        with self.assertRaises(VideoError): validate_video_size(VideoOutput("https://x/v", "video/mp4", 101), 100)

    def test_ssrf_addresses_rejected(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"):
            with self.subTest(address=address), self.assertRaises(VideoError):
                validate_remote_video_url("https://example.com/v.mp4", [address])

    def test_https_global_address_allowed(self):
        validate_remote_video_url("https://media.example/v.mp4", ["93.184.216.34"])

    def test_selector_is_explicitly_video(self):
        entries = video_selector_entries()
        self.assertTrue(all(item["id"].startswith("video:") for item in entries))
        self.assertTrue(all(item["name"].startswith("LF Video · ") for item in entries))

    def test_cancel_is_an_explicit_client_operation(self):
        self.assertTrue(hasattr(FakeClient(), "cancel"))
