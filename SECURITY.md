# Security

## Stage 1 state

No Lumenfall key exists in this project or was requested during Stage 1. No real
Lumenfall request was made. Tests use fixed fake values and mock transports.

The live Open WebUI deployment has a configured `WEBUI_SECRET_KEY`, but
`ENABLE_VALVE_ENCRYPTION` is not explicitly set. In `v0.11.3` its default is
`False`. A password-style Valve masks the UI only; it does not encrypt the value
at rest. Therefore a production key must not be added to the Valve until the
Stage 2 secret-storage decision is explicitly approved.

## Secret abstraction

`LumenfallClient` depends on `KeyProvider`, not directly on a Valve. The current
`ValveKeyProvider` is a prototype implementation. Stage 2 should choose one of:

1. deliberately enable and operationally validate Open WebUI Valve encryption,
   understanding that stable `WEBUI_SECRET_KEY` retention becomes a recovery
   dependency; or
2. add a narrowly mounted, read-only secret file and a minimal provider that
   reads only that fixed path.

Do not infer encryption from masked UI rendering. Do not put a key in Git,
Compose text, logs, tests, chat history, screenshots, or exception messages.

## Network and paid-job controls

- The Lumenfall origin is a source constant, not administrator-supplied input.
- Only `/images/generations` is called.
- `n=1` is fixed in this version.
- Any non-null `__task__` returns before key access or HTTP construction.
- `pipes()` is entirely local and cannot spend money.
- Missing keys fail closed.
- Timeouts are bounded.

## Media controls

- Base64 length is bounded before allocation and decoded length is checked again.
- Supported signatures: PNG, JPEG, GIF, WebP, and AVIF.
- A supplied MIME type must agree with detected bytes.
- The default decoded limit is 25 MiB and is Valve-configurable from 1–100 MiB.
- Image bytes go to Open WebUI storage; they are not embedded in message text.

## Ownership and authentication

The persistence adapter forwards only the current request's public
authentication material to Open WebUI's own in-process public API. It does not
store a second token, mint an administrator credential, or accept a broad
service key. The uploaded file therefore receives the current user's normal
ownership and access controls.

If no current-user Authorization/API-key header or cookie is available, the
adapter fails rather than falling back to an administrator identity.

## Logging and errors

The Pipe does not log prompts, bodies, base64, headers, or keys. Upstream error
bodies are never passed through. Normalized messages cover HTTP 400, 401, 402,
404, 429, 502, timeout, connectivity, malformed JSON, missing media, invalid
base64, unexpected MIME type, and oversized media.
