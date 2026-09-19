# Open WebUI Lumenfall Media Pipe

An image-only Open WebUI manifold Pipe that exposes a small administrator-curated
set of Lumenfall models as stable `LF Image · …` entries in the normal model
selector.

This repository is a **Stage 1 prototype**. It has not been imported, enabled, or
given a Lumenfall key in production. Video and image editing are out of scope.

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

`MODEL_LIST` accepts one entry per line. A friendly name is optional:

```text
gemini-3-pro-image | Gemini 3 Pro Image
vertex/gemini-3-pro-image | Gemini 3 Pro Image (Vertex)
```

Commas are also accepted for simple lists. Provider-prefixed IDs are preserved
unchanged. Prices and a provider catalogue are intentionally not embedded.

## Current status

The prototype test suite passes against the pinned Open WebUI image. A synthetic
1×1 PNG also passed a live, in-process public-file-API upload/read/delete proof.
No production Function, setting, secret, paid request, or retained test artifact
was created.

See `INSTALL.md` before any later production work.
