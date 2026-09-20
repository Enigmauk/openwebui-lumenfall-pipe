# Lumenfall Video Stage 1

Research date: 2026-09-20. This is development evidence, not a deployment
record. No video Function was imported and no real video was generated.

## Canonical API contract

The authoritative API reference uses the OpenAI-compatible base
`https://api.lumenfall.ai/openai/v1`:

- submit: `POST /videos` (JSON or multipart), success `202 Accepted`;
- status: `GET /videos/{id}`;
- cancel: `DELETE /videos/{id}`, success `204 No Content`, best effort only;
- authentication: `Authorization: Bearer ...`;
- states: `queued`, `in_progress`, `completed`, `failed`;
- completed output: `output.url`, `output.content_type`, `output.size_bytes`;
- output expiry: `expires_at`; the URL is therefore transport, not persistence;
- failure: `error.code` and `error.message`;
- routing/cost: `metadata.provider`, `provider_name`, `upstream_id`, `model`,
  `executed_model`, `cost_estimate`, final `cost`, and `cost_currency`;
- dry run: `?dryRun=true`, documented as validation/cost estimation without
  execution or balance impact;
- duplicate protection: request-body `idempotency_key`, up to 256 characters;
- T2V and I2V share the endpoint; I2V uses `input_reference`.

The current model pages sometimes label their video base as `/v1` and show a
conceptual `/v1/videos/generations`, while their actual examples post to
`/v1/videos`. The dedicated API reference and current OpenAI SDK guide agree on
`/openai/v1/videos`, so the prototype treats that as canonical. The published
OpenAPI download is currently a Mintlify placeholder containing only sample
`/plants` paths and cannot resolve this discrepancy.

Documented common fields are `prompt`, `model`, `seconds` (alias `duration`),
`size`, fixed future `n=1`, `aspect_ratio`, `resolution`, `input_reference`,
`negative_prompt`, `media_retention`, `webhook_url`, `idempotency_key`,
`metadata`, and `user`. Additional provider fields pass through. Frame rate,
audio, and seed are not normalized common fields in the current API reference.

No separate video-content endpoint is documented: completed media is downloaded
from the expiring HTTPS output URL. Cancellation does not promise a refund or
that work was not billed. No automatic submission retry is safe after an
ambiguous response; reconcile using the stable idempotency key/request records.

## Lifecycle architecture decision

The synchronous polling Pipe is technically possible: v0.11.3 awaits an async
`pipe()` inside its SSE response and status events can keep Cloudflare Tunnel
streaming. It is not robust for all shortlisted models. Current observed p95
latencies reach several minutes, the browser may abort its request on reload or
close, Open WebUI propagates stream cancellation, and the paid Lumenfall job can
continue independently. A live event emitter is supplementary, not durable job
ownership.

No documented v0.11.3 Function facility provides a durable, restart-safe,
per-user job queue that can later download, upload, and attach a file. Ad-hoc
`asyncio` tasks, application state, direct database writes, and internal storage
helpers are rejected. A manual submit/check workflow is safe but poor selector
UX and makes recovery user-dependent.

**Selected production direction: a small local worker, with the Pipe as the
authenticated submit/status facade.** The Pipe creates and durably records one
idempotency key before submission; the worker owns polling, download, and the
public Open WebUI upload/event completion path. Stage 2 must design its narrow
authenticated callback and persistent ownership mapping before building it.
This is the smallest design that remains correct across browser disconnects,
Open WebUI request cancellation, and service restarts.

Cloudflare's default proxied HTTP read timeout is 125 seconds, but Tunnel streams
`text/event-stream` rather than buffering it. That helps an active Pipe, yet does
not solve browser disconnects or durable recovery, so it is not the deciding
guarantee.

## Polling and duplicate-charge design

The prototype submits exactly once, requires an idempotency key, and converts a
submit timeout/connection loss into an explicit ambiguous state. It never
blindly re-POSTs. Once an ID exists, bounded GET retries are allowed. Polling
starts after 2 seconds, normally checks every 5 seconds, backs off to 15 seconds,
and has a 15-minute default observation deadline. A deadline preserves the job
ID and does not cancel or resubmit. Terminal failure, unknown state, malformed
payload, and absent job ID fail closed. Cancellation is a separate explicit
operation and remains best effort.

## Download and persistence design

Stage 2 should stream to a mode-0600 temporary file, never buffer a normal video
fully in memory. Only HTTPS port 443 is accepted. Resolve every hostname, reject
loopback/private/link-local/multicast/reserved addresses, connect with bounded
timeouts, disable automatic redirects, and independently revalidate each
redirect target and resolved address. Limit redirect count; verify final
`Content-Type` and MP4/WebM signature; enforce both declared and streamed byte
limits; fsync/close before upload; always remove the temporary file.

Upload remains through public `POST /api/v1/files/?process=false` under the
originating user, followed by a persistent `files` event/reference. Never retain
the Lumenfall URL or base64 in chat. The public route accepts generic files, but
inline playback of a synthetic MP4 has not yet been proven on v0.11.3. Stage 2
must test whether the `files` event renders an inline player or generic
attachment; if HTML is needed, it must reference only
`/api/v1/files/{id}/content`.

The host filesystem currently has about 914 GiB free. Capacity is not the only
limit: uploads traverse Open WebUI and browsers. Start with a **256 MiB maximum
per generated clip**, one output, and short curated durations; revisit using
real provider size evidence. This comfortably exceeds the documented 15 MiB
example without making multi-gigabyte downloads possible.

## Exploratory models

Initial T2V shortlist (current catalogue facts are ephemeral, never application
logic):

- `p-video`: cheap/fast development target; T2V/I2V, up to 10 s, Replicate;
- `wan-2.6`: Chinese quality/value baseline; T2V/I2V/V2V, 720p/1080p,
  Alibaba, observed median around 71 s;
- `seedance-2.0`: leading Chinese quality; T2V/I2V/V2V, native audio,
  480p/720p/1080p, up to 15 s, fal, observed median around 158 s;
- `kling-v3`: Chinese cinematic alternative; T2V/I2V, 720p, optional native
  audio/voice control and negative prompt, up to 15 s, fal/Replicate, observed
  median around 155 s.

Comparison-only candidates include `sora-2` (T2V/I2V, 720p, up to 20 s) and
the current Veo family. Current Hailuo/MiniMax and newer Wan variants should be
re-queried before Stage 2; they were not sufficiently established from the
current first-party pages to enter this initial default. `VIDEO_MODEL_LIST` is
separate and entries are explicitly encoded as `video:<model-id>` with
`LF Video · ...` labels; image/video modality is never guessed.

## Prototype and remaining work

`lumenfall_video.py` contains no production Function import and no real HTTP
client. It provides job/status/output data classes, response validation, an
exactly-once submission boundary, bounded async polling, explicit cancellation
abstraction, separated video selector entries, size checks, and initial SSRF
URL/IP rejection. Mock tests cover success/failure lifecycles, transient GET
failure, ambiguous POST, timeout with known ID, internal-task rejection before
submit, malformed/missing job data, unknown state, MIME/size/SSRF controls, and
selector separation. The existing image implementation is unchanged.

Authenticated dry runs were not performed: Stage 3 image work had just observed
an authenticated 403, so repeating calls would not add reliable evidence. Stage
2 should first resolve/reconcile that account/API behavior, then dry-run a few
representative video models without generation.

