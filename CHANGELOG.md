# Changelog

## 0.2.0-estimator-source — 2026-09-21

- Audited the running pinned Open WebUI `v0.11.3` Tool, Function, Action,
  permission, Valve, import/export and server-side execution paths.
- Added `lumenfall_cost_estimator.py`, a self-contained source-only Workspace
  Tool with shared image/video request specifications, curated administrator
  allowlists, exact integer-micros costs, sequential five-model comparisons and
  per-model results.
- Added a fixed-origin dry-run-only client with mandatory `dryRun=true`, normal
  TLS verification, `trust_env=False`, redirects disabled, bounded timeout and
  response parsing, sanitized errors, and fail-closed execution/media checks.
- Added the future exact leading `/estimate` Pipe command design without
  modifying the deployed image Pipe source.
- Added 25 estimator tests. The complete 148-test suite passed inside the exact
  pinned image with networking disabled and source mounted read-only. Python
  compilation and `git diff --check` also passed.
- Bounded authenticated validation made three image and three video dry-run
  calls. All returned confirmed estimates with no job/media fields. Balance was
  `$0.999` before and after. No real generation, production import, deployment,
  Function change, worker change, Compose change or Open WebUI configuration
  change occurred.

The Workspace Tool and `/estimate` command remain undeployed source work.

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
