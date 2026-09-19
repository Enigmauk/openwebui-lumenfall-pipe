import base64
import json
import unittest
from types import SimpleNamespace

import httpx

from lumenfall_pipe import (
    GeneratedImage,
    IMAGE_GENERATIONS_URL,
    INTERNAL_TASK_RESPONSE,
    LumenfallClient,
    LumenfallPipeError,
    OpenWebUIPublicFileAdapter,
    Pipe,
    decode_image,
    detect_image_content_type,
    extract_prompt,
    parse_generation_response,
    parse_model_list,
    selected_model_id,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_B64 = base64.b64encode(PNG).decode()


class StaticKeyProvider:
    def __init__(self, key="stage1-test-key"):
        self.key = key

    def get_key(self):
        return self.key


def response_payload():
    return {
        "data": [{"b64_json": PNG_B64}],
        "metadata": {
            "cost": 0.04,
            "cost_currency": "USD",
            "provider": "vertex",
            "provider_name": "Google Vertex AI",
            "executed_model": "vertex/gemini-image-test",
        },
    }


class ModelAndPromptTests(unittest.IsolatedAsyncioTestCase):
    def test_model_list_with_friendly_names_and_commas(self):
        entries = parse_model_list("model-a | Model A\nvertex/model-b | Model B, model-c")
        self.assertEqual(
            [(entry.model_id, entry.display_name) for entry in entries],
            [("model-a", "Model A"), ("vertex/model-b", "Model B"), ("model-c", "model-c")],
        )

    def test_duplicate_models_keep_first_label(self):
        entries = parse_model_list("model-a | First\nmodel-a | Second")
        self.assertEqual(entries[0].display_name, "First")
        self.assertEqual(len(entries), 1)

    def test_provider_prefix_is_preserved(self):
        self.assertEqual(parse_model_list("vertex/gemini-image")[0].model_id, "vertex/gemini-image")

    def test_invalid_model_id_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_model_list("not a model")

    async def test_selector_labels(self):
        pipe = Pipe()
        pipe.valves.MODEL_LIST = "model-a | Friendly\nvertex/model-b | Forced"
        self.assertEqual(
            await pipe.pipes(),
            [
                {"id": "model-a", "name": "LF Image · Friendly"},
                {"id": "vertex/model-b", "name": "LF Image · Forced"},
            ],
        )

    async def test_model_list_does_not_require_network_or_key(self):
        pipe = Pipe()
        pipe.valves.LUMENFALL_API_KEY = ""
        pipe.valves.MODEL_LIST = "model-a | Always visible"
        pipe._transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("offline")))
        self.assertEqual((await pipe.pipes())[0]["id"], "model-a")

    def test_selected_manifold_model(self):
        entries = parse_model_list("vertex/model-a | A")
        self.assertEqual(selected_model_id("lumenfall_media.vertex/model-a", entries), "vertex/model-a")

    def test_selected_model_with_dots(self):
        entries = parse_model_list("flux.2-max | Flux")
        self.assertEqual(selected_model_id("lumenfall_media.flux.2-max", entries), "flux.2-max")

    def test_unconfigured_model_is_rejected(self):
        with self.assertRaisesRegex(LumenfallPipeError, "not configured"):
            selected_model_id("pipe.other", parse_model_list("allowed"))

    def test_plain_string_prompt(self):
        body = {"messages": [{"role": "user", "content": "  draw a lighthouse  "}]}
        self.assertEqual(extract_prompt(body), "draw a lighthouse")

    def test_structured_content_prompt(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "draw"},
                        {"type": "image_url", "image_url": {"url": "ignored"}},
                        {"type": "text", "text": "a lighthouse"},
                    ],
                }
            ]
        }
        self.assertEqual(extract_prompt(body), "draw\na lighthouse")

    def test_metadata_prompt_is_preferred(self):
        body = {"messages": [{"role": "user", "content": "wrapped text"}]}
        self.assertEqual(extract_prompt(body, {"user_prompt": "original prompt"}), "original prompt")

    def test_no_user_prompt(self):
        with self.assertRaisesRegex(LumenfallPipeError, "provide a text prompt"):
            extract_prompt({"messages": [{"role": "assistant", "content": "hello"}]})


class TaskProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_documented_internal_task_is_non_billable(self):
        documented_tasks = [
            "title_generation",
            "follow_up_generation",
            "tags_generation",
            "emoji_generation",
            "query_generation",
            "image_prompt_generation",
            "autocomplete_generation",
            "function_calling",
            "moa_response_generation",
            "context_compaction",
            "memory_review",
        ]
        pipe = Pipe()
        pipe.valves.LUMENFALL_API_KEY = ""
        for task in documented_tasks:
            with self.subTest(task=task):
                result = await pipe.pipe({}, __task__=task)
                self.assertEqual(result, INTERNAL_TASK_RESPONSE)

    async def test_unknown_future_internal_task_is_also_non_billable(self):
        pipe = Pipe()
        self.assertEqual(await pipe.pipe({}, __task__="future_internal_task"), "")


class DecodingTests(unittest.TestCase):
    def test_successful_png_decode(self):
        image = decode_image({"b64_json": PNG_B64}, 1024)
        self.assertEqual(image.data, PNG)
        self.assertEqual(image.content_type, "image/png")

    def test_invalid_base64(self):
        with self.assertRaisesRegex(LumenfallPipeError, "invalid base64"):
            decode_image({"b64_json": "%%%%"}, 1024)

    def test_missing_image_data(self):
        with self.assertRaisesRegex(LumenfallPipeError, "no image data"):
            decode_image({}, 1024)

    def test_unsupported_content_type(self):
        raw = base64.b64encode(b"not-an-image").decode()
        with self.assertRaisesRegex(LumenfallPipeError, "unsupported or invalid"):
            decode_image({"b64_json": raw}, 1024)

    def test_declared_content_type_mismatch(self):
        with self.assertRaisesRegex(LumenfallPipeError, "unexpected image content type"):
            decode_image({"b64_json": PNG_B64, "content_type": "image/jpeg"}, 1024)

    def test_encoded_oversize_is_rejected_before_decode(self):
        with self.assertRaisesRegex(LumenfallPipeError, "larger than"):
            decode_image({"b64_json": base64.b64encode(PNG * 20).decode()}, 32)

    def test_decoded_oversize_is_enforced(self):
        with self.assertRaisesRegex(LumenfallPipeError, "larger than"):
            decode_image({"b64_json": PNG_B64}, len(PNG) - 1)

    def test_image_magic_types(self):
        samples = {
            b"\xff\xd8\xffrest": "image/jpeg",
            b"GIF89arest": "image/gif",
            b"RIFF\x00\x00\x00\x00WEBPrest": "image/webp",
            b"\x00\x00\x00\x18ftypavifrest": "image/avif",
        }
        for data, expected in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(detect_image_content_type(data), expected)

    def test_cost_and_provider_metadata(self):
        result = parse_generation_response(response_payload(), 1024)
        self.assertEqual(result.metadata.cost, 0.04)
        self.assertEqual(result.metadata.provider, "vertex")
        self.assertEqual(result.metadata.provider_name, "Google Vertex AI")
        self.assertEqual(result.metadata.executed_model, "vertex/gemini-image-test")


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_fails_closed(self):
        client = LumenfallClient(key_provider=StaticKeyProvider(""), timeout_seconds=1, max_image_bytes=1024)
        with self.assertRaisesRegex(LumenfallPipeError, "no API key"):
            await client.generate(model="model-a", prompt="hello")

    def test_request_construction_omits_empty_size(self):
        self.assertEqual(
            LumenfallClient.build_request("model-a", "hello"),
            {"model": "model-a", "prompt": "hello", "n": 1, "response_format": "b64_json"},
        )

    def test_request_construction_includes_configured_size(self):
        request = LumenfallClient.build_request("model-a", "hello", "1024x1024")
        self.assertEqual(request["size"], "1024x1024")
        self.assertEqual(request["response_format"], "b64_json")

    async def test_generation_request_and_response(self):
        def handler(request):
            self.assertEqual(str(request.url), IMAGE_GENERATIONS_URL)
            self.assertEqual(request.method, "POST")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "vertex/model-a")
            self.assertEqual(body["response_format"], "b64_json")
            self.assertNotIn("size", body)
            return httpx.Response(200, json=response_payload())

        client = LumenfallClient(
            key_provider=StaticKeyProvider(),
            timeout_seconds=1,
            max_image_bytes=1024,
            transport=httpx.MockTransport(handler),
        )
        result = await client.generate(model="vertex/model-a", prompt="hello")
        self.assertEqual(result.images[0].data, PNG)

    async def test_normalized_http_errors(self):
        expected = {
            400: "rejected",
            401: "authentication failed",
            402: "insufficient balance",
            404: "unavailable",
            429: "rate limiting",
            502: "All Lumenfall providers",
        }
        for status, message in expected.items():
            with self.subTest(status=status):
                transport = httpx.MockTransport(
                    lambda request, status=status: httpx.Response(
                        status,
                        json={"error": {"message": "private prompt and stage1-test-key"}},
                    )
                )
                client = LumenfallClient(
                    key_provider=StaticKeyProvider(),
                    timeout_seconds=1,
                    max_image_bytes=1024,
                    transport=transport,
                )
                with self.assertRaisesRegex(LumenfallPipeError, message) as caught:
                    await client.generate(model="model-a", prompt="private prompt")
                self.assertNotIn("stage1-test-key", str(caught.exception))
                self.assertNotIn("private prompt", str(caught.exception))

    async def test_malformed_json(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"not-json"))
        client = LumenfallClient(
            key_provider=StaticKeyProvider(), timeout_seconds=1, max_image_bytes=1024, transport=transport
        )
        with self.assertRaisesRegex(LumenfallPipeError, "malformed JSON"):
            await client.generate(model="model-a", prompt="hello")

    async def test_upstream_timeout(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        client = LumenfallClient(
            key_provider=StaticKeyProvider(),
            timeout_seconds=1,
            max_image_bytes=1024,
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaisesRegex(LumenfallPipeError, "timed out"):
            await client.generate(model="model-a", prompt="hello")


class FakePersistence:
    def __init__(self):
        self.calls = []

    async def persist(self, *, image, filename, request):
        self.calls.append((image, filename, request))
        return {
            "type": "image",
            "id": "file-test",
            "url": "/api/v1/files/file-test/content",
            "name": filename,
            "content_type": image.content_type,
        }


class PersistenceAndPipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_adapter_requires_current_user_auth(self):
        adapter = OpenWebUIPublicFileAdapter()
        request = SimpleNamespace(app=object(), headers={}, cookies={})
        with self.assertRaisesRegex(LumenfallPipeError, "authenticated request"):
            await adapter.persist(
                image=GeneratedImage(PNG, "image/png"), filename="test.png", request=request
            )

    async def test_pipe_uses_persistence_adapter_and_files_event(self):
        pipe = Pipe()
        pipe.valves.MODEL_LIST = "vertex/model-a | Model A"
        pipe.valves.LUMENFALL_API_KEY = "stage1-test-key"
        pipe._transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_payload()))
        persistence = FakePersistence()
        pipe._persistence = persistence
        events = []

        async def emit(event):
            events.append(event)

        request = SimpleNamespace()
        result = await pipe.pipe(
            {"model": "lumenfall_media.vertex/model-a", "messages": [{"role": "user", "content": "draw"}]},
            __event_emitter__=emit,
            __request__=request,
        )
        self.assertEqual(result, "The generated image is attached to this message.")
        self.assertEqual(len(persistence.calls), 1)
        files_events = [event for event in events if event["type"] == "files"]
        self.assertEqual(files_events[0]["data"]["files"][0]["id"], "file-test")
        self.assertNotIn("b64_json", json.dumps(events))

    async def test_interactive_context_is_required_before_generation(self):
        pipe = Pipe()
        with self.assertRaisesRegex(LumenfallPipeError, "interactive"):
            await pipe.pipe({"model": "x", "messages": []})


if __name__ == "__main__":
    unittest.main()
