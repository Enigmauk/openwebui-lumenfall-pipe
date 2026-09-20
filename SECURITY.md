# Security

## Secret storage

The Lumenfall key is never stored in this project, Compose text, a Valve, or the
Open WebUI database. `ENABLE_VALVE_ENCRYPTION` remains unchanged. The host file
is `/opt/stacks/openwebui/secrets/lumenfall-api-key`, protected by a `0700`
directory and `0600` file, and mounted as the single read-only container file
`/run/secrets/lumenfall-api-key`.

## Secret abstraction

`LumenfallClient` depends on `KeyProvider`, not directly on storage. The
production `SecretFileKeyProvider` reads only the fixed path and returns empty
on missing/unreadable files, making generation fail closed.

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
- Authenticated cost checks use Lumenfall's documented `dryRun=true` mode, which
  does not execute generation or affect account balance.

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
