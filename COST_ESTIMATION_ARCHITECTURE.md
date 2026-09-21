# Lumenfall cost estimation architecture

Status: source design for Open WebUI v0.11.3. Nothing in this document is
deployed.

## Decision

Use two user entry points over one request-building and dry-run core:

1. A Workspace Tool gives ordinary text models explicit single-model and
   multi-model image/video estimate functions. The text model explains the
   returned structured data; Lumenfall remains the only price source.
2. A future exact leading `/estimate` command in each Lumenfall Pipe quotes the
   request for the currently selected model. It must use the same request
   specification as generation and must never fall through to generation.

The initial source artifact is `lumenfall_cost_estimator.py`. It is deliberately
self-contained because Open WebUI v0.11.3 stores and executes each Workspace
Tool or Function as a separate Python source blob. A Function cannot safely
import another database-backed Tool module. The reusable request specifications
live in the estimator artifact and can be consumed directly by repository code;
a later Pipe deployment must bundle the same core into its independently
imported single-file artifact rather than import the live Tool record.

Do not modify the deployed image Pipe during this checkpoint.

## Pinned v0.11.3 findings

- A Workspace Tool is a `class Tools` object loaded with `exec()` and cached in
  the Open WebUI backend process. Its public typed methods become OpenAI-style
  tool schemas and execute server-side.
- Read access is controlled through Tool ownership and access grants. Creation,
  import, export, source updates, access changes and deletion have separate
  backend permission checks. A Tool has no Function-style global active toggle;
  availability comes from access plus selection/assignment to a chat or model.
- Admin `Valves` are stored with the Tool record. `UserValves` are stored per
  user. Valve encryption depends on the installation-wide encryption setting,
  so this design does not store the Lumenfall key in a Valve.
- Tool calls receive model-supplied parameters after schema filtering. Reserved
  values such as `__user__`, `__messages__`, `__files__` and request/event
  objects are injected only when named by the method. The estimator methods do
  not name or forward conversation, file, cookie, user or request context.
- The Tool runs in the same container process that has the existing exact
  read-only `/run/secrets/lumenfall-api-key` mount. A read-only live check found
  the file readable without a Compose change.
- Pipes are selectable model providers. Internal task metadata is injected by
  the server; the existing image Pipe rejects every non-null task before secret
  or network access.
- Action Functions are enabled server-side message-toolbar buttons. They
  receive the selected message and broad chat context and are associated with
  global or model-specific Function configuration. They add useful explicit
  click UX but do not replace the estimator API or multi-model reasoning.
- Tool and Function import/export persist source and metadata in Open WebUI's
  database. Functions additionally have active/global toggles. Tool modules are
  reloaded when stored source changes; deleting a Tool removes its cache entry.

Current online first-party documentation broadly agrees with these findings,
but the pinned container source is authoritative for compatibility.

## Shared request specifications

`ImageRequestSpec` builds the exact image generation payload: requested model,
prompt, `n=1`, `response_format=b64_json`, and an optional explicit `size`.

`VideoRequestSpec` builds the eventual text-to-video payload: requested model,
prompt, `n=1`, and only explicitly supplied `seconds`, `size`, `resolution` or
`aspect_ratio`. No duration, dimensions or aspect ratio are silently converted.
Model defaults remain omitted.

Generation and estimation must serialize these specifications. There is no
local pricing formula or catalogue price table.

## Dry-run safety boundary

The estimator client has only image/video estimate methods. It has no generation,
poll, download, cancel or arbitrary-URL method. Its two fixed request URLs
include `dryRun=true`; redirects and environment proxy inheritance are disabled;
TLS verification remains enabled; timeouts, connection limits and response
bytes are bounded.

An accepted response must be a bounded JSON object with `estimated` exactly
`true`, a non-empty returned model, a non-negative integer
`total_cost_micros`, a three-letter currency and an optional provider. Any job,
status, output, media or image-generation marker fails closed even if
`estimated` is also present. The client never polls, downloads or retries via a
non-dry-run request. Upstream bodies are not copied into errors.

Costs remain integer micros plus currency. Decimal display text is derived only
for convenience.

## Curated model policy

Administrator `Valves` hold separate image and video allowlists in the same
`model-id | display name` format as the Pipes. Defaults are reconciled with the
current 14-model image Pipe list and four-model development video shortlist,
but changing one live record does not silently change another. The Tool rejects
model IDs outside its current allowlist before key access or HTTP.

`list_estimate_models(media_type)` is included because it lets a calling model
resolve friendly names to exact curated IDs without a catalogue network call.
No catalogue lookup occurs during Tool discovery or estimation.

## Tool API

- `estimate_image(prompt: str, model: str, size: str = "")`
- `compare_image_models(prompt: str, models: list[str], size: str = "")`
- `estimate_video(prompt: str, model: str, seconds: int | None = None,
  size: str = "", resolution: str = "", aspect_ratio: str = "")`
- `compare_video_models(prompt: str, models: list[str], seconds: int | None =
  None, size: str = "", resolution: str = "", aspect_ratio: str = "")`
- `list_estimate_models(media_type: str)`

Descriptions state that these functions estimate only and are for explicit
cost, quote, price or comparison requests. The methods accept no hidden chat or
file parameters.

Comparisons accept at most five distinct models and execute sequentially. Each
model has an independent success/error result. An execution/media anomaly is
the exception: it stops the comparison immediately. Successful results retain
the requested and returned model, provider, exact integer cost, currency,
request payload and any recognized effective/default parameters returned by
Lumenfall.

Video results track duration and dimensions as independent default groups. A
request with explicit seconds but no size/resolution/aspect ratio still reports
that dimensions use model defaults and the prices may not be directly
comparable. Explicit settings are sent unchanged to every requested model. A
model-specific rejection remains that model's error; the Tool never substitutes
another duration, size, resolution or aspect ratio.

## Future exact Pipe command

Only a leading command matching `/estimate` followed by whitespace and a
non-empty prompt invokes estimation. Matching is case-sensitive and exact.
Prompts that merely contain “estimate”, “price” or “cost” follow normal image
generation behavior.

For the image Pipe, the command removes only the command prefix and uses the
selected model plus the Pipe's configured optional size. Estimate failure is a
terminal estimate error. Non-null `__task__` still exits before command parsing,
key access or HTTP. The existing generation/persistence branch stays unchanged.

For video, prefer ordinary Open WebUI-native model selection plus the existing
structured `video_options` fields. The first command form should be simply
`/estimate <prompt>` and quote those explicit options or model defaults. Avoid a
second parameter mini-language unless a deployed UI proves structured options
cannot be supplied safely.

## Alternatives

- **Action button:** promising later as an explicit “Estimate with Lumenfall”
  convenience. Deferred because it receives broader chat context, needs a
  prompt/model selection interaction, and duplicates the Tool without improving
  the core safety boundary.
- **Separate estimator Pipe:** rejected initially. It adds another model to the
  selector and makes natural-language multi-model comparison less ergonomic
  than a Tool attached to an existing text model.
- **OpenAPI/MCP/local service:** deferred. It would improve reuse outside Open
  WebUI and process isolation, but adds a service, authentication boundary,
  secret mount, network policy, deployment and backup surface without a current
  need.
- **Dynamic catalogue browsing:** deferred. Catalogue facts can help an admin
  maintain allowlists, but discovery-time network calls make selector/tool
  availability fragile and do not replace request-specific dry runs.
- **Automatic cheapest-model selection, generation after estimate, and budget
  automation:** deferred. Each needs separate policy and approval design. An
  estimate never authorizes generation.
