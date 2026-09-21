# Testing

Tests never call Lumenfall. HTTP behavior uses `httpx.MockTransport` and tiny
in-memory fixtures.

The cost-estimator coverage includes confirmed image/video dry runs, sequential
multi-model comparisons, five-model and curated-model enforcement, partial
failures, explicit-option preservation, model-default reporting, malformed and
unconfirmed estimates, unexpected execution/media payloads, sanitized HTTP and
transport failures, missing keys, exact integer micros, prompt/context
isolation, and proof that the estimator exposes no generation/fallback method.

On 2026-09-21 the complete suite passed **148/148** in the exact pinned
Open WebUI image with `--network none`, a read-only source mount and mocked HTTP
for every automated test.

## Bounded estimator live validation

After the no-network suite passed, a disposable pinned-image container used the
existing key mounted read-only and the new estimator source. The balance read
was `$0.999` before and after. Three image dry runs returned confirmed estimates
for Seedream 5 Lite (35,000 micros), Qwen Image 2512 (20,000 micros), and
FLUX.2 Max (70,000 micros). Three explicit five-second video dry runs returned
confirmed estimates for P-Video (100,000 micros), Wan 2.6 (500,000 micros), and
Seedance 2.0 (1,517,000 micros). All currencies were USD.

No response contained job, execution, output or media fields. No poll, download,
generation, production import or configuration change occurred. Temporary
validation scripts were removed.

## Automated suite

Run in an environment containing the same `httpx` and `pydantic` versions as
the target Open WebUI image:

```bash
python -m unittest discover -v
```

For the current Docker deployment, copy the repository to a temporary container
path, run the command there, and delete that temporary copy afterward. Do not
copy it into Open WebUI's Function database or data directory for Stage 1.

Current Lumenfall Video Stage 2 Checkpoint C result: **72 tests passed** inside
the exact `v0.11.3` image with container networking disabled: all 54 prior image
and video-prototype tests plus 18 durable-worker tests.

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
- the 14-entry Stage 2 default catalogue and fixed secret-file provider;
- authenticated dry-run query construction and estimate validation.
- durable SQLite job creation and persistence across worker reconstruction;
- idempotency-key persistence before the one allowed submit attempt;
- duplicate logical intent reuse and changed-intent conflict handling;
- ambiguous-submit fail-closed recovery without a second submit;
- queued, in-progress, completed, failed, transient-poll and cancellation paths;
- restart after submit, during polling, and before mock persistence;
- encrypted prompt/session material and credential-refresh delivery recovery;
- authenticated health/job routes and cross-user job isolation.

The Checkpoint C worker tests use only deterministic fake video and persistence
backends. The test container uses `--network none`; no Lumenfall endpoint,
production secret, production Open WebUI API, or paid generation is reachable.

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

## Production integration checks

- no internal task causes an outbound request;
- selector entries exist during simulated Lumenfall outage;
- a generated file belongs to the requesting non-admin user;
- another user cannot read it without a grant;
- reload retains the assistant attachment;
- chat JSON contains a file reference and no base64 payload;
- deleting a test chat/file follows expected Open WebUI semantics;
- timeout and every normalized provider error render cleanly;
- server logs contain no key, Authorization header, base64, or full prompt.

## Stage 2 production result

The disabled-first production import matched the Git source SHA-256 exactly.
A separately named temporary Pipe used the same persistence boundary with an
in-memory `httpx.MockTransport` and a 1×1 PNG. The current user's file was
stored through the public API, attached to the assistant message, survived a
reload, and was absent from message/chat JSON as base64. Unauthenticated file
access returned 401. The installation had no second ordinary user, so a true
cross-user denial could not be exercised without creating an unrelated account;
that result remains covered by the public route's access-control design review.
The temporary chat, Function, file, scripts, and import JSON were deleted.

Authenticated `dryRun=true` requests succeeded for five representative models
with no explicit size and no generation. See `STAGE2_REPORT.md` for the
sanitized provider and estimate results.
