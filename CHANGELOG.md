# Changelog

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
