import inspect
import json
import unittest

import httpx

from lumenfall_cost_estimator import (
    DEFAULT_IMAGE_MODEL_LIST,
    DEFAULT_VIDEO_MODEL_LIST,
    IMAGE_DRY_RUN_URL,
    VIDEO_DRY_RUN_URL,
    DryRunEstimator,
    ImageRequestSpec,
    Tools,
    VideoRequestSpec,
    parse_model_list,
)


class StaticKeyProvider:
    def __init__(self, key="estimator-test-key"):
        self.key = key
        self.calls = 0

    def get_key(self):
        self.calls += 1
        return self.key


def estimate_payload(model="model-a", micros=100_000, **extra):
    return {
        "estimated": True,
        "model": model,
        "provider": "test-provider",
        "total_cost_micros": micros,
        "currency": "USD",
        "components": [],
        **extra,
    }


def configured_tool(handler, *, image_models="model-a | Model A\nmodel-b | Model B", video_models=None):
    tool = Tools()
    tool.valves.IMAGE_MODEL_LIST = image_models
    tool.valves.VIDEO_MODEL_LIST = video_models or image_models
    tool._key_provider_factory = StaticKeyProvider
    tool._transport = httpx.MockTransport(handler)
    return tool


class RequestSpecificationTests(unittest.TestCase):
    def test_default_allowlists_reconcile_with_current_pipes(self):
        self.assertEqual(len(parse_model_list(DEFAULT_IMAGE_MODEL_LIST)), 14)
        self.assertEqual(
            [entry.model_id for entry in parse_model_list(DEFAULT_VIDEO_MODEL_LIST)],
            ["p-video", "wan-2.6", "seedance-2.0", "kling-v3"],
        )

    def test_image_payload_matches_generation_shape(self):
        self.assertEqual(
            ImageRequestSpec("model-a", " exact prompt ", "1024x1024").to_payload(),
            {
                "model": "model-a",
                "prompt": " exact prompt ",
                "n": 1,
                "response_format": "b64_json",
                "size": "1024x1024",
            },
        )

    def test_video_payload_preserves_explicit_options(self):
        self.assertEqual(
            VideoRequestSpec("p-video", "prompt", 5, resolution="720p").to_payload(),
            {"model": "p-video", "prompt": "prompt", "n": 1, "seconds": 5, "resolution": "720p"},
        )


class SingleEstimateTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_single_estimate(self):
        def handler(request):
            self.assertEqual(str(request.url), IMAGE_DRY_RUN_URL)
            self.assertEqual(request.method, "POST")
            self.assertEqual(
                json.loads(request.content),
                {
                    "model": "model-a",
                    "prompt": "a lighthouse",
                    "n": 1,
                    "response_format": "b64_json",
                    "size": "1024x1024",
                },
            )
            return httpx.Response(200, json=estimate_payload("model-a", 12_345))

        result = await configured_tool(handler).estimate_image(
            "a lighthouse", "model-a", "1024x1024"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["total_cost_micros"], 12_345)
        self.assertEqual(result["formatted_cost"], "$0.012345")
        self.assertFalse(result["uses_model_defaults"])

    async def test_video_single_estimate(self):
        def handler(request):
            self.assertEqual(str(request.url), VIDEO_DRY_RUN_URL)
            body = json.loads(request.content)
            self.assertEqual(body["seconds"], 5)
            self.assertEqual(body["aspect_ratio"], "16:9")
            self.assertEqual(body["n"], 1)
            return httpx.Response(
                200,
                json=estimate_payload(
                    "p-video", 100_000, effective_parameters={"seconds": 5, "resolution": "720p"}
                ),
            )

        result = await configured_tool(handler, video_models="p-video | P-Video").estimate_video(
            "a short pan", "p-video", seconds=5, aspect_ratio="16:9"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "test-provider")
        self.assertEqual(result["effective_parameters"], {"seconds": 5, "resolution": "720p"})
        self.assertEqual(result["formatted_cost"], "$0.10")

    async def test_video_omitted_options_are_marked_as_model_defaults(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=estimate_payload("p-video"))
        )
        estimator = DryRunEstimator(key_provider=StaticKeyProvider(), transport=transport)
        result = await estimator.estimate_video(VideoRequestSpec("p-video", "prompt"))
        structured = result.to_dict()
        self.assertTrue(structured["uses_model_defaults"])
        self.assertIn("may not be directly comparable", structured["comparison_note"])


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_multi_image_comparison_is_sequential_and_exact(self):
        requested = []

        def handler(request):
            body = json.loads(request.content)
            requested.append(body["model"])
            return httpx.Response(200, json=estimate_payload(body["model"], len(requested) * 1_000))

        result = await configured_tool(handler).compare_image_models(
            "same prompt", ["model-a", "model-b"], size="1024x1024"
        )
        self.assertTrue(result["ok"])
        self.assertEqual(requested, ["model-a", "model-b"])
        self.assertEqual([item["total_cost_micros"] for item in result["results"]], [1_000, 2_000])

    async def test_multi_video_comparison(self):
        def handler(request):
            body = json.loads(request.content)
            return httpx.Response(200, json=estimate_payload(body["model"], 50_000))

        result = await configured_tool(handler).compare_video_models(
            "same clip", ["model-a", "model-b"], seconds=5, resolution="720p"
        )
        self.assertEqual([item["requested_model"] for item in result["results"]], ["model-a", "model-b"])
        self.assertTrue(all(item["request"]["seconds"] == 5 for item in result["results"]))

    async def test_model_count_limit_is_enforced_before_http(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=estimate_payload())

        models = [f"model-{index}" for index in range(6)]
        tool = configured_tool(handler, image_models="\n".join(models))
        result = await tool.compare_image_models("prompt", models)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "too_many_models")
        self.assertEqual(calls, 0)

    async def test_curated_model_enforcement_precedes_key_and_http(self):
        provider = StaticKeyProvider()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=estimate_payload())

        tool = configured_tool(handler)
        tool._key_provider_factory = lambda: provider
        result = await tool.estimate_image("prompt", "hallucinated-model")
        self.assertEqual(result["error"]["code"], "model_not_allowed")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(calls, 0)

    async def test_partial_failure_does_not_fail_comparison(self):
        def handler(request):
            model = json.loads(request.content)["model"]
            if model == "model-a":
                return httpx.Response(400, json={"error": {"message": "private upstream detail"}})
            return httpx.Response(200, json=estimate_payload(model, 2_000))

        result = await configured_tool(handler).compare_image_models(
            "comparison prompt", ["model-a", "model-b"]
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["results"][0]["ok"])
        self.assertTrue(result["results"][1]["ok"])
        self.assertNotIn("private upstream detail", json.dumps(result))

    async def test_unsupported_video_parameter_combination_fails_before_http(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=estimate_payload())

        result = await configured_tool(handler).compare_video_models(
            "prompt", ["model-a"], size="1280x720", resolution="720p"
        )
        self.assertEqual(result["error"]["code"], "unsupported_parameters")
        self.assertEqual(calls, 0)

    async def test_execution_anomaly_stops_comparison_immediately(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(202, json={"id": "unexpected-job", "status": "queued"})

        result = await configured_tool(handler).compare_video_models(
            "prompt", ["model-a", "model-b"], seconds=5
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["stopped_early"])
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(calls, 1)


class FailClosedTests(unittest.IsolatedAsyncioTestCase):
    def estimator(self, handler, key="estimator-test-key"):
        return DryRunEstimator(
            key_provider=StaticKeyProvider(key), transport=httpx.MockTransport(handler)
        )

    async def test_malformed_estimate_json(self):
        estimator = self.estimator(lambda request: httpx.Response(200, content=b"not-json"))
        with self.assertRaisesRegex(Exception, "malformed estimate JSON"):
            await estimator.estimate_image(ImageRequestSpec("model-a", "prompt"))

    async def test_estimated_true_is_required(self):
        estimator = self.estimator(
            lambda request: httpx.Response(200, json={**estimate_payload(), "estimated": False})
        )
        with self.assertRaisesRegex(Exception, "did not confirm"):
            await estimator.estimate_image(ImageRequestSpec("model-a", "prompt"))

    async def test_job_and_media_responses_fail_closed(self):
        payloads = [
            {**estimate_payload(), "id": "video_job", "status": "queued"},
            {**estimate_payload(), "data": [{"b64_json": "not-real-media"}]},
            {**estimate_payload(), "output": {"url": "https://media.invalid/video.mp4"}},
        ]
        for payload in payloads:
            with self.subTest(keys=list(payload)):
                estimator = self.estimator(lambda request, payload=payload: httpx.Response(200, json=payload))
                with self.assertRaisesRegex(Exception, "execution or media data"):
                    await estimator.estimate_video(VideoRequestSpec("model-a", "prompt"))

    async def test_accepted_job_status_fails_without_reading_or_polling(self):
        calls = []

        def handler(request):
            calls.append((request.method, str(request.url)))
            return httpx.Response(202, json={"id": "video_job", "status": "queued"})

        with self.assertRaisesRegex(Exception, "accepted a job"):
            await self.estimator(handler).estimate_video(VideoRequestSpec("model-a", "prompt"))
        self.assertEqual(calls, [("POST", VIDEO_DRY_RUN_URL)])

    async def test_http_errors_are_sanitized(self):
        for status in (401, 402, 403, 404, 429, 500, 502):
            with self.subTest(status=status):
                estimator = self.estimator(
                    lambda request, status=status: httpx.Response(
                        status,
                        json={
                            "error": {
                                "message": "private prompt estimator-test-key",
                                "code": "PRIVATE_PROVIDER_CODE",
                            }
                        },
                    )
                )
                with self.assertRaises(Exception) as caught:
                    await estimator.estimate_image(ImageRequestSpec("model-a", "private prompt"))
                message = str(caught.exception)
                self.assertNotIn("private prompt", message)
                self.assertNotIn("estimator-test-key", message)
                self.assertNotIn("PRIVATE_PROVIDER_CODE", message)

    async def test_timeout_and_network_errors_are_sanitized(self):
        errors = [httpx.ReadTimeout("secret timeout"), httpx.ConnectError("secret network")]
        for error in errors:
            def handler(request, error=error):
                error.request = request
                raise error

            with self.subTest(error=type(error).__name__), self.assertRaises(Exception) as caught:
                await self.estimator(handler).estimate_image(ImageRequestSpec("model-a", "prompt"))
            self.assertNotIn("secret", str(caught.exception))

    async def test_no_key_fails_before_http(self):
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=estimate_payload())

        estimator = self.estimator(handler, key="")
        with self.assertRaisesRegex(Exception, "key is unavailable"):
            await estimator.estimate_image(ImageRequestSpec("model-a", "prompt"))
        self.assertEqual(calls, 0)

    async def test_no_generation_method_or_fallback(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(500, json={"error": {"message": "fail"}})

        estimator = self.estimator(handler)
        self.assertFalse(hasattr(estimator, "generate"))
        self.assertFalse(hasattr(estimator, "submit"))
        with self.assertRaises(Exception):
            await estimator.estimate_image(ImageRequestSpec("model-a", "prompt"))
        self.assertEqual(calls, [IMAGE_DRY_RUN_URL])
        self.assertIn("dryRun=true", calls[0])

    async def test_unexpected_tool_error_is_sanitized(self):
        tool = configured_tool(lambda request: httpx.Response(200, json=estimate_payload()))

        def broken_factory():
            raise RuntimeError("private prompt estimator-test-key")

        tool._key_provider_factory = broken_factory
        result = await tool.estimate_image("private prompt", "model-a")
        self.assertEqual(result["error"]["code"], "internal_error")
        self.assertNotIn("private prompt", json.dumps(result))
        self.assertNotIn("estimator-test-key", json.dumps(result))

    async def test_cost_micros_are_retained_exactly(self):
        exact = 9_007_199_254_740_993
        estimator = self.estimator(
            lambda request: httpx.Response(200, json=estimate_payload("model-a", exact))
        )
        result = await estimator.estimate_image(ImageRequestSpec("model-a", "prompt"))
        self.assertEqual(result.total_cost_micros, exact)
        self.assertIsInstance(result.total_cost_micros, int)

    async def test_prompt_and_context_isolation(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json=estimate_payload("model-a"))

        tool = configured_tool(handler)
        result = await tool.estimate_image("only this prompt", "model-a")
        self.assertTrue(result["ok"])
        self.assertEqual(
            set(captured), {"model", "prompt", "n", "response_format"}
        )
        for method_name in (
            "estimate_image",
            "compare_image_models",
            "estimate_video",
            "compare_video_models",
        ):
            parameters = inspect.signature(getattr(Tools, method_name)).parameters
            self.assertFalse(any(name.startswith("__") for name in parameters))


class ModelListingTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_models_is_read_only_and_uses_exact_ids(self):
        tool = Tools()
        result = await tool.list_estimate_models("video")
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["model"] for item in result["models"]],
            ["p-video", "wan-2.6", "seedance-2.0", "kling-v3"],
        )
