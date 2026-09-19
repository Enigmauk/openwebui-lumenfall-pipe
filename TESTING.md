# Testing

Tests never call Lumenfall. HTTP behavior uses `httpx.MockTransport` and tiny
in-memory fixtures.

## Automated suite

Run in an environment containing the same `httpx` and `pydantic` versions as
the target Open WebUI image:

```bash
python -m unittest discover -v
```

For the current Docker deployment, copy the repository to a temporary container
path, run the command there, and delete that temporary copy afterward. Do not
copy it into Open WebUI's Function database or data directory for Stage 1.

Current Stage 1 result: **34 tests passed** inside the exact `v0.11.3` image.

Coverage includes:

- curated manifold entries, friendly labels, duplicate handling, dots, and
  provider-prefixed IDs;
- selector parsing and rejection of unconfigured models;
- string, structured-array, and metadata prompt extraction;
- no-prompt behavior;
- every documented internal `__task__` plus unknown future task names;
- fixed endpoint, `n=1`, optional size, and `response_format=b64_json`;
- response metadata;
- 400, 401, 402, 404, 429, 502, timeout, connectivity-safe handling,
  malformed JSON, missing data, invalid base64, MIME mismatch, unsupported
  bytes, and size limits;
- key/prompt redaction from errors;
- persistence interface use and persistent `files` event shape;
- selector availability when Lumenfall is unreachable.

## Stage 1 live synthetic persistence proof

On 2026-09-20 a temporary script imported the exact running Open WebUI app and
used `httpx.ASGITransport` to call the public API as the existing user. It:

1. uploaded a 1×1 PNG with `POST /api/v1/files/?process=false` (HTTP 200);
2. read file metadata (HTTP 200);
3. read the content (HTTP 200) and matched every byte;
4. deleted the file through the public API (HTTP 200);
5. confirmed subsequent lookup returned 404;
6. confirmed zero matching database rows and no matching upload file remained.

The short-lived authentication token existed only inside the test process and
was never printed or stored. This was an Open WebUI persistence test, not a
Lumenfall request.

## Mandatory pre-production integration checks

- no internal task causes an outbound request;
- selector entries exist during simulated Lumenfall outage;
- a generated file belongs to the requesting non-admin user;
- another user cannot read it without a grant;
- reload retains the assistant attachment;
- chat JSON contains a file reference and no base64 payload;
- deleting a test chat/file follows expected Open WebUI semantics;
- timeout and every normalized provider error render cleanly;
- server logs contain no key, Authorization header, base64, or full prompt.
