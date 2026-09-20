# Open WebUI Lumenfall Media Pipe

An image-only Open WebUI manifold Pipe that exposes a small administrator-curated
set of Lumenfall models as stable `LF Image · …` entries in the normal model
selector.

This repository contains the reviewed image-only v0.1 Function used for the
Stage 2 production deployment. Video and image editing remain out of scope, and
the first paid generation is deliberately deferred to Stage 3.

## Design summary

- Compatibility target: Open WebUI `v0.11.3`, commit
  `2a960a59fe1dbbd35282f0556b3666d81102e781` only.
- Integration: in-process async Pipe Function/manifold, not legacy Pipelines.
- Model discovery: curated `MODEL_LIST` Valve; no network request from `pipes()`.
- Lumenfall endpoint: fixed
  `https://api.lumenfall.ai/openai/v1/images/generations` with
  `response_format=b64_json`.
- Persistence: Open WebUI's public `POST /api/v1/files/?process=false` API over
  `httpx.ASGITransport`, authenticated with the current request, followed by a
  documented persistent `files` event.
- Paid-job protection: any non-`None` `__task__` returns before key lookup,
  network access, or persistence.
- Secret: fixed read-only file `/run/secrets/lumenfall-api-key`; no key Valve.
- Cost validation: authenticated `?dryRun=true` requests are supported by the
  client without exposing a generating selector action.
- Dependencies: only Python's standard library plus `httpx` and `pydantic`, both
  already present in the target Open WebUI image. There is no frontmatter
  `requirements` declaration.

## Repository map

- `lumenfall_pipe.py` — importable Open WebUI Function and testable core.
- `tests/` — no-paid-call automated tests.
- `ARCHITECTURE.md` — data flow, boundaries, and deliberate trade-offs.
- `COMPATIBILITY.md` — exact upstream review and version-sensitive surfaces.
- `SECURITY.md` — key handling, ownership, limits, and threat boundaries.
- `INSTALL.md` — later reviewed installation/update/rollback procedure.
- `TESTING.md` — automated and future integration checks.
- `STAGE1_REPORT.md` — sanitized live audit and persistence-spike evidence.

## Curated model configuration

`MODEL_LIST` accepts one entry per line. A friendly name is optional. The
production default has 14 intentionally broad exploratory choices:

```text
seedream-5-lite | Seedream 5 Lite
seedream-4.5 | Seedream 4.5
qwen-image-2512 | Qwen Image 2512
qwen-image | Qwen Image
qwen-image-max | Qwen Image Max
wan-2.7 | Wan 2.7
wan-2.6 | Wan 2.6
z-image-turbo | Z-Image Turbo
flux.2-klein-4b | FLUX.2 Klein 4B
flux.2-klein-9b | FLUX.2 Klein 9B
flux.2-dev-flash | FLUX.2 Dev Flash
flux.2-max | FLUX.2 Max
grok-imagine-image | Grok Imagine
grok-imagine-image-pro | Grok Imagine Pro
```

Commas are also accepted for simple lists. Provider-prefixed IDs are preserved
unchanged. Prices and a provider catalogue are intentionally not embedded.

Prices are never embedded in generation logic. FLUX.2 Dev Flash was selected
over Dev Turbo for the initial list because current Lumenfall data makes Flash
the cheaper, faster exploratory tier; Wan 2.7 Pro was omitted as the closest
high-cost/high-latency duplicate. The Valve remains editable without a source
change, and provider-prefixed IDs remain valid.

See `INSTALL.md` for production, update, and rollback procedures.
