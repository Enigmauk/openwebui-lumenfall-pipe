# Compatibility

## Pinned target

- Open WebUI tag: `v0.11.3`
- Commit: `2a960a59fe1dbbd35282f0556b3666d81102e781`
- Production image digest reviewed:
  `sha256:751b617714b91e4cfd0186a509c72480c858e012976103b09a30dad053c36175`
- Review date: 2026-09-20

No compatibility is claimed for any other Open WebUI version.

## Exact source findings

### Pipe/manifold contract

`backend/open_webui/functions.py`:

- `get_function_models()` accepts `pipes` as a list, sync function, or async
  function and creates IDs as `<function-id>.<sub-id>`.
- `generate_function_chat_completion()` obtains the function ID by splitting
  the selected model on the first dot.
- `get_function_params()` passes only reserved arguments explicitly named by
  the Pipe signature.
- `__task__` comes from server-created request metadata; `__request__` is the
  FastAPI request; `__event_emitter__` is created for requests with session,
  chat, and message IDs.

The public Pipe documentation agrees that async Pipes are preferred, manifold
IDs may themselves contain dots after the first separator, and failures in
`pipes()` remove all selector entries. The prototype therefore keeps `pipes()`
local and deterministic.

### Global native image-model limitation

`backend/open_webui/routers/images.py`:

- `CreateImageForm` has `model: str | None`.
- `image_generations()` nevertheless sets `model = await get_image_model(request)`.
- Its OpenAI payload uses that global/configured `model`, not
  `form_data.model`.
- The Automatic1111 branch is the one that conditionally mutates the global
  model for administrators.

Therefore native OpenAI-compatible image configuration cannot safely provide
per-selector Lumenfall model switching in this version. This Pipe sends the
curated model directly and never mutates global image configuration.

### Public file persistence

`backend/open_webui/routers/files.py`:

- `POST /api/v1/files/` is handled by `upload_file()` and requires a verified
  current user.
- `process=false` skips document extraction while retaining storage, hashing,
  file metadata, user ownership, and configured storage-provider behavior.
- `DELETE /api/v1/files/{id}` enforces owner/admin/write access.

`backend/open_webui/socket/main.py` persists a `files` event into assistant
message metadata for saved chats. `src/lib/components/chat/Chat.svelte` handles
both `files` and `chat:message:files` in the live UI.

The version-sensitive boundary is limited to:

1. public file route path/query/response fields;
2. availability of `__request__.app` to an explicitly declared Pipe argument;
3. current-user authentication being present in the request header or cookies;
4. documented `files` event shape and file-content URL.

Regression tests and the synthetic persistence check must cover these before an
upgrade.

### Function dependencies and Valves

- `httpx` and `pydantic` are present in `v0.11.3`; the Function declares no
  extra requirements.
- `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` defaults to `True` in
  `backend/open_webui/env.py`, but is irrelevant because this Function has no
  requirements field.
- `ENABLE_VALVE_ENCRYPTION` defaults to `False`.
- `backend/open_webui/utils/valves.py` stores Valve objects unchanged when
  encryption is disabled and uses Fernet derived from `WEBUI_SECRET_KEY` only
  when enabled.
- `STORAGE_PROVIDER` defaults to `local` in
  `backend/open_webui/config.py`.

## Lumenfall documented boundary

First-party documentation reviewed:

- https://docs.lumenfall.ai/api-reference/images/generate
- https://docs.lumenfall.ai/api-reference/introduction
- https://docs.lumenfall.ai/api-reference/cost-estimation
- https://docs.lumenfall.ai/api-reference/models/list
- https://docs.lumenfall.ai/routing

The code relies only on documented behavior: fixed OpenAI-compatible base URL,
Bearer authentication, `POST /images/generations`, `response_format=b64_json`,
`data[].b64_json`, normalized error statuses, and `metadata.cost`,
`cost_currency`, `provider`, `provider_name`, and `executed_model`.

Provider-forced IDs such as `fal/<model>` are passed through unchanged.
Lumenfall routing and fallback are not reproduced locally. Stage 2 implements
the documented `?dryRun=true` cost-estimation request and validates only the
documented `estimated`, `model`, `provider`, `total_cost_micros`, and `currency`
fields. It does not expose dry run as a selector entry.

## Upgrade gate

Before changing Open WebUI versions:

1. review release notes and migration guidance;
2. compare all source boundaries listed above;
3. run the complete unit suite inside the prospective image;
4. run the synthetic authenticated ASGI file upload/read/delete test;
5. import into a non-production instance and verify selector, generation mock,
   event attachment, reload persistence, access control, and cleanup;
6. only then schedule the production Open WebUI upgrade.

If the public route/event design stops working, document the failure before
considering an internal helper. The smallest current fallback would be
`upload_file_handler()` in `backend/open_webui/routers/files.py`, as used by
`upload_image()` in `backend/open_webui/routers/images.py`; it is intentionally
not imported by this project.
