# Architecture

## Goal and boundary

The Mini PC orchestrates remote Lumenfall image generation. It performs no local
inference. Existing OpenRouter text models are untouched. This version does not
implement video, image editing, reference inputs, dynamic catalogues, pricing, or
Lumenfall routing.

## Request flow

1. Open WebUI calls `pipes()` to build selector entries solely from the
   administrator's `MODEL_LIST` Valve.
2. On `pipe()`, any non-null `__task__` returns an empty internal-task response
   immediately. This fail-closed rule also covers task names introduced later.
3. The selected manifold suffix is matched against the curated list. Arbitrary
   unconfigured model IDs are rejected.
4. The prompt comes first from documented `__metadata__["user_prompt"]`, then
   falls back to the last user message. Plain strings and OpenAI content arrays
   are supported.
5. The client sends one async request to Lumenfall's fixed image endpoint with
   `n=1` and `response_format=b64_json`. `size` is omitted unless configured.
6. The response is bounded before and after base64 decoding, validated by file
   signature, and reduced to image bytes plus documented routing/cost metadata.
7. `OpenWebUIPublicFileAdapter` posts the bytes to
   `/api/v1/files/?process=false` through an in-process `httpx.ASGITransport`
   using the current request's Authorization/API-key header or cookies.
8. The returned user-owned file ID is emitted as a documented persistent
   `files` event. The assistant text contains only a short acknowledgement, not
   base64.

## Why the model list is curated

Lumenfall's documented basic `/models` objects expose `id`, `object`, `created`,
and `owned_by`, but not a dependable modality field. A live catalogue fetch in
`pipes()` would also remove every manifold entry during an outage and expose far
more models than wanted. The local Valve is deterministic, outage-independent,
and editable without changing source.

## Persistence decision

The supported public file API is sufficient on Open WebUI `v0.11.3`.

The adapter deliberately does not import Open WebUI database models, storage
providers, `upload_file_handler`, `upload_image`, or chat mutation helpers. The
ASGI transport avoids external network traversal while still exercising the
normal authenticated public route, storage provider, ownership checks, file
record creation, and size policy. The documented persistent `files` event saves
the returned file reference on the assistant message and updates the UI.

No undocumented internal persistence API is required by version 0.1.

## Rejected shortcuts

- **Global image-model switching:** Open WebUI `v0.11.3` accepts a request model
  but its OpenAI generation branch obtains the effective model from global image
  configuration. Per-request global mutation would race between users and alter
  unrelated image generation.
- **Direct database/storage calls:** bypass public validation, ownership, storage
  abstraction, events, and future migrations.
- **Internal image helpers:** convenient but version-sensitive and unnecessary
  after the public-API proof.
- **Temporary Lumenfall URLs:** unsuitable for durable chat history.
- **Base64 in assistant content:** inflates chat JSON and bypasses normal file
  access controls.
- **Arbitrary base URL Valve:** creates an avoidable SSRF/credential-exfiltration
  surface.
- **Live `/models` in `pipes()`:** unreliable selector behavior and insufficient
  documented modality data.

## Replaceable abstractions

`KeyProvider` separates secret retrieval from generation. Version 0.1 uses
`SecretFileKeyProvider`, which reads only the fixed
`/run/secrets/lumenfall-api-key` path. The file is mounted individually and
read-only; there is no key Valve or configurable secret path.

`PersistenceAdapter` separates generation from Open WebUI persistence. Tests use
an in-memory fake; production uses only the public file API adapter.

## Failure behavior

User-visible errors are normalized and never include upstream bodies, request
headers, keys, or full prompts. Missing key, missing interactive request/event
context, invalid models, malformed responses, invalid/oversized media, known
HTTP statuses, transport failures, and timeouts fail closed.

## Cost estimation

`LumenfallClient.estimate()` sends the same bounded `n=1`, optional-size request
to the fixed endpoint with `?dryRun=true`. It validates the documented estimate
shape and returns only model, provider, currency, and cost micros. It is an
operator validation path, not a selector-visible generation mode. Normal image
responses supply provider/effective-cost metadata for the concise future status
`Lumenfall · Friendly Model · Provider · $0.000`; no price is hard-coded.
