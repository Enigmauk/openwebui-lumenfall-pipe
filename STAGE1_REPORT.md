# Stage 1 report

Date: 2026-09-20 (Europe/London)

## Live read-only audit

- Compose project: `openwebui`
- Compose working directory/file: `/opt/stacks/openwebui/compose.yaml`
- Container: `open-webui`
- Image: `ghcr.io/open-webui/open-webui:v0.11.3`
- Image/container digest:
  `sha256:751b617714b91e4cfd0186a509c72480c858e012976103b09a30dad053c36175`
- OCI source revision: `2a960a59fe1dbbd35282f0556b3666d81102e781`
- State: running and healthy
- Container user: `0:0`
- Data: host `/opt/stacks/openwebui/data` → container `/app/backend/data`,
  read/write bind mount
- Database: `/opt/stacks/openwebui/data/webui.db`
- Relevant filesystem: about 1,007 GiB total, 914 GiB available, 5% used at
  audit time
- Storage provider: no `STORAGE_PROVIDER` environment override; `v0.11.3`
  default and loaded implementation are local storage
- `WEBUI_SECRET_KEY`: configured; value not read or displayed
- `ENABLE_VALVE_ENCRYPTION`: not explicitly set; effective `v0.11.3` default is
  `False`
- `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`: not explicitly set; effective
  `v0.11.3` default is `True`

### Image configuration

- Image generation: disabled
- Engine field: `openai`
- Model: empty
- OpenAI-compatible base URL: default `https://api.openai.com/v1`
- OpenAI image key: not configured
- Automatic1111, ComfyUI, and Gemini image credentials/base URLs: not
  configured
- Image editing: disabled
- Existing configured image size: `512x512`
- Image-prompt generation: enabled, although overall image generation is off

No image setting was changed.

### Existing Functions/Pipes

Safe metadata showed one active global Function:

- ID `token_usage_display`, name `Token Usage Display`, type `filter`

No Pipe Function was present. Function source and Valve values were not read.

### Backup mechanism

The established encrypted Restic mechanism is active through
`minipc-restic-backup.timer` (daily), with weekly maintenance and integrity-check
timers. Open WebUI is on the consistency-stop list. Retained standalone
pre-upgrade database/Compose backups and the protected pre-Open-Terminal backup
were present. No backup setting or artifact was changed.

## Compatibility review

Open WebUI tag `v0.11.3` was shallow-cloned and verified at exact commit
`2a960a59fe1dbbd35282f0556b3666d81102e781`, matching the running image label.
See `COMPATIBILITY.md` for source locations and findings.

Current official Open WebUI documentation confirms that separate Pipelines are
legacy for new work, Pipe Functions can expose manifolds, async handlers are
preferred, special arguments include `__task__`, `__event_emitter__`, and
`__request__`, and `files` is a persistent event.

Current first-party Lumenfall documentation confirms the fixed OpenAI-compatible
base, image endpoint, `b64_json`, normalized errors, dry run, provider-prefixed
IDs, and response cost/provider metadata. Its basic `/models` response does not
document sufficiently rich modality data for the selector requirement.

## Architecture result

Selected: async in-process manifold Pipe + curated Valve model list + fixed
Lumenfall client + public Open WebUI file API over in-process ASGI + persistent
`files` event.

The public API is sufficient on the pinned version. No internal Open WebUI module,
database mutation, source patch, monkey patch, global image setting change, or
extra permanent credential is required.

## Persistence experiment

The live synthetic public-API proof succeeded completely and cleaned up after
itself. See `TESTING.md`. This establishes storage, database record creation,
current-user authentication, readback, and deletion. Source review and automated
event tests establish that `files` is persisted on the assistant message and
rendered by the UI.

## Prototype and tests

- Local repository: `/opt/stacks/openwebui-lumenfall-pipe`
- Scope: image generation only
- Automated result: 34/34 passed inside the pinned production image
- Real Lumenfall calls: zero
- Production imports/enables/config changes: zero

## Decisions remaining before Stage 2

1. Choose and approve encrypted Valve storage or a fixed read-only secret file.
2. Choose the initial curated image models and friendly labels.
3. Decide whether to leave size unset per model or configure a default.
4. Decide the decoded image-size limit if 25 MiB is not desired.
5. Decide whether a documented dry run is required before the first paid call.
6. Set a first-live-test budget and confirm Lumenfall account controls.
7. Run the non-production import/UI/reload/access-control test matrix.
8. Take a verified pre-install Open WebUI backup and confirm Function/Valve
   recovery under the chosen secret design.
9. Review and approve production installation as a separate stage.
