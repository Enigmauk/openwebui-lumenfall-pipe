# Lumenfall Video Stage 2 — worker architecture

Status: Checkpoints B and C are implemented; Checkpoint E has a mock-validated
HTTP client and result downloader, and Checkpoint G has a mock-validated
development Pipe-to-worker flow. Nothing has been deployed or imported into
Open WebUI as a video component; the existing image Pipe remains unchanged. No
production configuration was changed, and no runtime Lumenfall video request
was made. Checkpoint D remains partially verified and incomplete; Checkpoint F
remains blocked.

## Scope and boundary

The intended flow is:

1. The authenticated Open WebUI Pipe validates the selected `video:<model>`
   entry and rejects internal `__task__` calls before invoking the worker.
2. The Pipe sends one request to a small local worker over a private Docker
   connection, with a dedicated worker Bearer secret and the current user's
   session credential. The Lumenfall key is never sent to the Pipe.
3. The worker writes a durable SQLite job record before contacting Lumenfall.
   It owns the one initial submit attempt, polling, download, recovery, and
   persistence bookkeeping.
4. Once complete, the worker uses the originating user's credential against
   Open WebUI's public chat and file APIs. The video is stored as a normal
   user-owned file and attached to the originating assistant message.
5. The Pipe can later retrieve state by local job ID. The worker never returns
   a temporary Lumenfall URL as the durable result.

This remains a design for a later separately approved deployment. The worker
port must not be published on the host or routed through Cloudflare. Only the
Pipe should be configured as an API caller; internal network location alone is
not authorization. The worker API requires a dedicated secret distinct from
the Lumenfall key. Any later Compose/network changes require separate review.

## Narrow worker API

The small internal interface is:

- `GET /health` — authenticated liveness and SQLite availability only; no job
  information.
- `POST /jobs` — validate one selected model and a saved chat/message, durably
  create or return the logical job, then schedule its single initial submit.
- `GET /jobs/{local_job_id}` — return sanitized state only when the caller's
  verified Open WebUI user ID owns the job.
- `DELETE /jobs/{local_job_id}` — record a cancellation request for an owned
  job; any upstream DELETE is best effort.
- `POST /jobs/{local_job_id}/delivery-credential` — optionally replace an
  expired/invalid current-user credential after the Pipe re-authenticates the
  owner. This does not submit or resubmit a Lumenfall job.

All routes require the separate worker Bearer secret. The Pipe supplies the
owner ID from Open WebUI's authenticated request context; clients cannot set an
arbitrary owner through Pipe inputs. Every read, cancel, or credential refresh
also checks the local job's owner ID. Job IDs are random opaque UUIDs, not
authorization tokens. The worker uses fixed Open WebUI and Lumenfall service
origins; callers cannot provide a URL, filesystem path, Lumenfall key, or
provider endpoint.

## Durable state model

Use one SQLite row per logical job, with a uniqueness constraint on
`(owner_user_id, chat_id, assistant_message_id)`. A repeated request for that
same assistant message returns its existing job if its canonical request
fingerprint matches; a different request for the same message is a conflict.
A genuinely new assistant message is a new user intent.

Minimum columns:

| Group | Stored fields |
| --- | --- |
| Identity | local job UUID; owner user ID; chat ID; assistant message ID; selected friendly ID; upstream model ID |
| Request safety | creation/update timestamps; HMAC request fingerprint; Lumenfall idempotency key; submit state/attempt timestamp; encrypted prompt/request only before the one submit attempt is resolved |
| Upstream lifecycle | Lumenfall job ID; last provider state; worker state; poll count; next poll time; sanitized error category; cancellation-requested flag |
| Result | provider; executed model; final cost and currency; MIME; expected and downloaded byte counts; worker-relative artifact path; final Open WebUI file ID; completion timestamp |
| Ownership continuation | encrypted current-user session credential and local expiry time, if needed for unattended delivery |

The prompt is encrypted while a new job is waiting to submit. Once Lumenfall
accepts it, the prompt ciphertext is erased; it is not needed to poll or deliver
the result. If submission becomes ambiguous, erase the prompt too and retain
only the random idempotency key and keyed request fingerprint for reconciliation.
Never log prompts, bearer values, media bytes, raw upstream bodies, or raw
authorization headers. Store only sanitized error categories and documented
provider/cost metadata.

Use parameterized SQL, explicit transactions, a uniqueness constraint, and a
local persistent volume for the database. Commit the idempotency key, encrypted
pending request, and `submitting` state before the network POST. Store media in
a private worker data directory, never a web-served path. Use restrictive
directory/file permissions and an atomic `.part`-to-final rename after media
validation. No Open WebUI or Lumenfall database is accessed directly.

## State machine and submission idempotency

The minimal local states are:

```text
pending_submit -> submitting -> queued -> in_progress
                                  |            |
                                  +------------+-> downloading -> persisting -> completed
                                                  \-> failed

submitting -> submit_ambiguous
queued/in_progress -> cancel_requested -> (continue observing until terminal)
downloading/persisting -> delivery_auth_required (local artifact/job is retained)
```

`failed`, `completed`, and `submit_ambiguous` do not re-enter submission.
Cancellation is a request, not proof of cancellation or refund; continue to
observe the known job where possible. The public video API documents the
`idempotency_key` body field and says sending the same key twice returns the
existing video. It does not publish a deduplication-retention window or
key/payload-conflict semantics. Therefore this worker makes one initial POST,
does not automatically retry an ambiguous POST, and never creates a new key
for a retry. This is intentionally conservative. A crash after committing
`submitting` but before the response is durably recorded becomes
`submit_ambiguous` on recovery; an operator must reconcile it rather than risk
a duplicate paid job. Once the upstream ID is stored, GET polling may be retried
with bounded backoff because it does not create a generation.

Before the POST, generate and commit one stable Lumenfall idempotency key. The
key belongs only to that local logical job and its immutable request. Repeated
Pipe calls for the same assistant message/fingerprint reuse the local job;
they do not dispatch another POST. No external retry middleware is permitted
around the create request.

## Restart and failure recovery

- A committed `pending_submit` job may be claimed once. A committed
  `submitting` job with no recorded upstream ID becomes `submit_ambiguous`;
  it is never automatically posted again.
- Rows with a known Lumenfall ID in `queued` or `in_progress` resume polling at
  their persisted `next_poll_at`. The 15-minute Pipe observation window does
  not delete, cancel, or resubmit the remote job; status remains recoverable.
- Terminal upstream `failed` and local `completed` rows remain terminal.
- A crash during download discards an incomplete `.part` file and retries the
  idempotent GET while the documented output URL is valid. A complete verified
  local artifact is kept for persistence recovery.
- Open WebUI upload has no documented idempotency-key field. Use the public
  `GET /api/v1/files/search` with a job-specific deterministic filename to find
  an upload that committed before a lost response. If the result is still
  ambiguous, preserve `persisting`/`delivery_auth_required` and do not
  blindly duplicate the upload.
- Before emitting a `files` event again, read the saved chat through the public
  chat API and check whether that file ID is already present on the target
  assistant message. After the event, verify the same public chat response
  contains the file ID before marking the local job `completed`.
- Poll retries, upload reconciliation, event reconciliation, and cancellation
  never change the Lumenfall idempotency key or call the create endpoint again.
- A worker restart scans active rows from SQLite; it does not rely on an
  in-memory task surviving. Failed or completed jobs are not restarted.

If a poll retry budget is exhausted, retain the known upstream ID and an
interrupted/poll-retry state for later bounded recovery. Do not classify a
temporary connectivity problem as a provider failure and do not resubmit.

## Open WebUI ownership and credential handling

The worker uses the current user's session JWT captured from the authenticated
Pipe invocation, not a permanent service/admin token and not an API key. The
Pipe may normalize the current `Authorization: Bearer` credential or the
`token` value of the current session cookie into that one JWT value; it never
passes or stores a whole `Cookie` header. The worker uses the credential only
with fixed, public Open WebUI routes:

1. Before paid submission, `GET /api/v1/chats/{chat_id}` must confirm that the
   current user can access the saved chat. Temporary/unsaved or non-owned chats
   fail closed before Lumenfall is contacted.
2. `POST /api/v1/files/?process=false` uploads the completed local bytes under
   that same session. The response must match the expected MIME, size, and
   current user owner before its file ID is recorded.
3. `GET /api/v1/files/search?filename=...&content=false` reconciles an upload
   whose response was lost, without database access.
4. `POST /api/v1/chats/{chat_id}/messages/{message_id}/event` emits the public
   persistent `files` event. Open WebUI `v0.11.3` verifies the caller and chat
   owner on this route. A public chat read verifies persisted attachment state
   before and after the event so recovery does not append duplicates.

At-rest bearer custody is a limited compromise required for a disconnected
browser: encrypt the single current-user credential with authenticated
encryption (AES-GCM) under a separate future worker secret, bind ciphertext to
the local job/owner as associated data, never store it as plaintext, and never
store a whole Cookie header. Proposed hard retention is six hours or until
terminal delivery, whichever comes first; an invalid/expired credential is
deleted immediately. A status call from the same owner may provide a fresh
current-user credential through the dedicated refresh route. If no valid
credential is available when delivery is ready, keep the protected artifact
and report `delivery_auth_required` for up to 24 hours; do not lose the known
video job or create another one. Delete the credential immediately after
delivery or terminal failure. This bounded lease and the refresh path must be
exercised in mock tests before any later deployment decision.

The worker never accepts a user ID as sufficient proof to upload as that user:
it uses the user-scoped Open WebUI credential for every file/chat operation,
and Open WebUI enforces file ownership and chat-owner access. The worker's
internal bearer secret restricts which local service can ask it to act. No
credentials are created, placed in Valves, or installed during Checkpoint B.

## First-party source references

- [Lumenfall video generation API](https://docs.lumenfall.ai/api-reference/videos/generate), [status](https://docs.lumenfall.ai/api-reference/videos/get), and [cancel](https://docs.lumenfall.ai/api-reference/videos/cancel) references.
- Pinned Open WebUI `v0.11.3` [file routes](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/routers/files.py), [chat event route](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/routers/chats.py), [auth helper](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/utils/auth.py), and [persistent event emitter](https://github.com/open-webui/open-webui/blob/v0.11.3/backend/open_webui/socket/main.py).

## Checkpoint E implementation boundary

`video_worker/lumenfall_http.py` implements the existing `VideoBackend`
interface with the fixed `https://api.lumenfall.ai` origin. The key comes only
from an injected key provider. Create uses the stored idempotency key and one
POST attempt; ambiguous transport outcomes and unusable 202 responses become
`submit_ambiguous`. Poll GET has three bounded attempts for transient failures,
strictly validates IDs/states/result metadata, and retains the known upstream
ID. DELETE is one best-effort request and does not claim cancellation or refund.
The explicit `create_http_worker_service` factory in
`video_worker/integrations.py` wires this client and the downloader without
changing the worker's deterministic fake defaults.

`video_worker/downloader.py` uses an injected transport and DNS resolver for
tests. It allows HTTPS URLs without userinfo, checks every literal or resolved
destination address for global routability before each request, rejects mixed
safe/unsafe DNS answers, and manually validates each redirect (maximum three).
It streams at most 256 MiB into a mode-`0600` `.part` file under a mode-`0700`
worker artifact directory. It checks declared and actual size, expected and
HTTP MIME, and MP4 `ftyp` or WebM EBML signature evidence before an atomic
rename. Failed downloads remove the partial file. The stored result contains
only sanitized provider/model/cost/MIME/size fields and a deterministic
worker-relative artifact name; the expiring output URL is never written to
SQLite. HTTPX request log URLs are redacted, and raised errors use fixed safe
categories.

The standard HTTP connector resolves a hostname again when opening the socket,
after the downloader's DNS validation. DNS validation is therefore not pinned
to the actual connection and a DNS-rebinding/TOCTOU window remains. Redirects
are independently revalidated, but this implementation does not claim to
eliminate that window. A future deployment review should decide whether a
connection-pinned transport is required for the threat model.

Checkpoint E validation ran all 98 tests (the prior 72 plus 26 new tests) in
the pinned Open WebUI `v0.11.3` image, with `--network none`, a read-only source
mount, mocked HTTP transports, and injected DNS resolvers. No authenticated
Lumenfall request, production key read, real DNS lookup, or real HTTP request
was part of the tests. Checkpoint D remains incomplete: uploaded-file
durability and cleanup were verified, while chat attachment rendering/history
reload and exact persisted representation/base64 absence remain unverified.
The HTTP 403 and Replay configuration are unresolved; Checkpoint F remains
blocked.

## Checkpoint G development-only integration

`lumenfall_video_pipe.py` is a separate Open WebUI Pipe facade; it does not
modify or import the deployed image Pipe. Its administrator-editable video
catalogue contains only explicit `video:<model-id>` selectors with `LF Video ·`
labels. Model prompts cannot set an owner, chat ID, message ID, worker origin,
or worker credential. The pinned v0.11.3 Function invocation supplies the
authenticated `__user__`, `__metadata__`, `__message_id__`, and `__request__`;
the facade derives ownership and saved-message identity from those fields and
normalizes only the bearer or `token` cookie value into the current user's
credential. It never forwards a Cookie header.

Any non-null `__task__`, including an unknown future task name, returns before
reading the body, identity, request credential, or worker-secret provider.
Invalid selectors, options, and missing/temporary chat or message identifiers
also fail before worker-client construction. The worker HTTP client uses the
fixed administrator-owned `http://lumenfall-video-worker:8000` origin and a
separate worker Bearer read lazily from `/run/secrets/lumenfall-video-worker-bearer`;
tests inject a fake Bearer provider and ASGI transport. The response parser is
bounded to 64 KiB and accepts only known worker states and sanitized fields.
The Pipe polls only for a small configured number of status checks. If the job
is still active, it returns its local job ID and status; it does not cancel or
resubmit. Status, cancellation, and delivery-credential refresh all use the
owner-scoped worker routes.

Before the worker claims a provider submission, its required
`SavedChatVerifier` collaborator checks that the initiating user owns the
saved chat/message using the single current-user credential. Missing or
negative verification fails closed without consuming the one-submit claim or
contacting the provider. Checkpoint G supplies a deterministic fake ownership
collaborator; a production Open WebUI ownership client has not been created or
validated. The persistence boundary receives the owner, chat, and assistant
message IDs together with the downloaded file so tests can prove attribution.
The Pipe emits the existing `files` event with a generic local file reference;
it makes no assertion about whether v0.11.3 renders that file inline, as a
video, or as an attachment. No base64 or expiring provider URL is emitted.

`tests/test_video_pipe.py` exercises the real Pipe client, FastAPI routes, and
worker service through ASGI transports, with deterministic fake provider,
synthetic MP4 download, fake saved-chat ownership, and fake persistence. It
covers duplicate reuse/conflict, owner isolation, ambiguous submission,
transient polling, provider/download failure, credential refresh, cancellation,
bounded Pipe observation, and service reconstruction. The downloader's DNS
resolver and every HTTP transport remain injected; tests perform no real DNS
or socket activity. This proves the development component boundaries only;
it does not establish production Open WebUI attachment rendering, deployment
configuration, secrets, or external connectivity.

## Current gates and implementation boundary

Checkpoint A found that Cloudflare returns HTTP 403 for the documented
read-only account and dry-run endpoints before a Lumenfall API JSON response;
the underlying cause and production Replay setting remain unverified. This
blocks authenticated Lumenfall API tests, not mock-only implementation. Stages
C–E and G must use only fake credentials/transports. Checkpoint F must remain
skipped until the 403 and account controls are resolved. No production video
Function import, worker Compose service, public port, Cloudflare route, paid
generation, or production image change is part of this design checkpoint.
