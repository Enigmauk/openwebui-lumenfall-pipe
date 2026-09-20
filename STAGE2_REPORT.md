# Stage 2 production report

Date: 2026-09-20

## Deployment

- Source commit deployed: `1be1d84303e8a0aafe74970a265e148bb5b2463f`
- Imported source SHA-256:
  `f94bf132f8b5b626cf959ea7aae0a417e007d36d45bae7be28ed7c23a6100ce3`
- Open WebUI remained pinned to `v0.11.3` and image digest
  `sha256:751b617714b91e4cfd0186a509c72480c858e012976103b09a30dad053c36175`.
- The supported JSON import created `lumenfall_media` as a disabled `pipe`.
  The record was verified byte-for-byte before enablement.
- No frontmatter requirements were present and no runtime dependency was
  downloaded.
- The only Compose change was a read-only single-file bind mount from the
  protected host secret to `/run/secrets/lumenfall-api-key`.
- Global Open WebUI image generation and image editing remained disabled.
  Existing OpenRouter text chat and Open Terminal remained operational.

The Lumenfall account was created with GitHub sign-in/OAuth. No API key, OAuth
token, cookie, session data, authentication URL, or secret characteristic is
recorded here.

## Backup

The protected rollback set is:

`/opt/stacks/openwebui/backups/pre-lumenfall-pipe-20260920T124512+0100`

It contains a verified online SQLite backup, pre-change Compose, image/digest
and source references, non-cache data archive, integrity result, and checksums.
All files are `0600` within a `0700` directory. The established encrypted Restic
job also completed successfully as snapshot `9c2f44a5` with repository metadata
verification.

## Persistence validation

A separately named temporary Pipe replaced only the upstream transport with an
in-memory synthetic response. It did not contact Lumenfall. The test verified:

- public current-user file upload and matching file/chat/message ownership;
- persistent assistant `files` metadata after page reload;
- exact stored PNG bytes;
- no base64 image in message or chat JSON;
- HTTP 401 for unauthenticated file access; and
- supported deletion of the temporary chat, Function, and file.

Only one Open WebUI account existed, so a second ordinary-user test was not
possible without creating unrelated durable account state. The test does not
claim that additional live-user result.

## Authenticated dry-run results

Lumenfall's documented `?dryRun=true` mode states that it validates parameters,
does not execute generation, and does not affect balance. All five requests
omitted size and were accepted:

| Model ID | Provider returned | Estimated USD |
| --- | --- | ---: |
| `flux.2-klein-4b` | `fal` | $0.005 |
| `seedream-5-lite` | `byteplus` | $0.035 |
| `qwen-image-2512` | `fal` | $0.020 |
| `wan-2.7` | `alibaba` | $0.030 |
| `grok-imagine-image` | `xai` | $0.020 |

These are estimates returned at validation time, not hard-coded prices or proof
of paid generation. Stage 2 made no paid media request.

## Remaining Stage 3 gate

Real image generation remains deliberately unverified. Stage 3 should begin
with one controlled `flux.2-klein-4b` request with expected cost below $0.01,
then compare selected Chinese, FLUX, and Grok models deliberately rather than
generating across the entire selector.
