# Changelog

## 0.1.0-stage2 — 2026-09-20

- Finalized a 14-model exploratory catalogue focused on Seedream, Qwen, Wan,
  Z-Image, FLUX.2, and Grok Imagine.
- Replaced the prototype Valve key with a fixed read-only secret-file provider
  at `/run/secrets/lumenfall-api-key`.
- Added authenticated, non-generating Lumenfall dry-run cost estimation support.
- Changed future success status text to use friendly model, returned provider,
  and returned effective cost metadata without hard-coded prices.
- Added secret-file, catalogue, and dry-run tests and updated production,
  security, architecture, testing, and rollback documentation.

No paid image generation is part of Stage 2.

## 0.1.0-stage1 — 2026-09-20

- Added an async image-only Lumenfall manifold Pipe for Open WebUI `v0.11.3`.
- Added curated Valve-based models with stable `LF Image · …` labels and
  provider-prefixed ID support.
- Added fail-closed protection for all internal `__task__` calls.
- Added a fixed-origin async Lumenfall client using `b64_json`, normalized errors,
  media validation, size limits, and routing/cost metadata parsing.
- Added current-user persistence through Open WebUI's public file API and
  documented persistent `files` events.
- Added 34 no-paid-call automated tests and compatibility/security/install docs.
- Confirmed the live synthetic persistence proof and removed its artifacts.

Not installed or enabled in production. No Lumenfall key or real request used.
