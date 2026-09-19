# Installation, update, and rollback

## Stage 1 stop

Do not install or enable this Function in production yet. Do not add a key,
change Valve encryption, modify Compose, add credit, or make a real request.

## Preconditions for a later reviewed Stage 2

1. Review `SECURITY.md` and approve a server-side key-storage method.
2. Re-run `TESTING.md` against the still-pinned production version.
3. Take and verify the normal pre-change Open WebUI backup.
4. Confirm available disk space and current storage provider.
5. Review the exact curated models and optional default size.
6. Decide whether a Lumenfall dry-run cost check will precede the first paid
   generation.
7. Define a small first-live-test budget and operator.

## Later installation outline

These steps are intentionally descriptive rather than authorization to execute:

1. In Open WebUI Admin → Functions, create a new Function from the reviewed
   `lumenfall_pipe.py` at a stable Python-identifier ID such as
   `lumenfall_media`.
2. Leave it disabled while configuring `MODEL_LIST`, size, timeout, and media
   limit.
3. Configure the approved key provider. A masked Valve is not acceptable while
   live Valve encryption remains disabled.
4. Enable the Function only after a non-paid/mock validation.
5. Verify selector labels, internal-task suppression, one tightly controlled
   generation, user ownership, assistant-message attachment, page reload,
   download/access behavior, logs, and charged cost metadata.
6. Verify the new durable state is included in the existing Open WebUI backup.

## Update

Review the diff and `CHANGELOG.md`, run all tests against the current pinned
image, back up Open WebUI, replace the Function source while disabled, inspect
server logs, re-enable, and repeat persistence/reload checks. Never combine a
Pipe update with an Open WebUI upgrade unless both rollback paths are explicit.

## Rollback

1. Disable the Function.
2. Restore the previously exported/reviewed Function source if rollback of code
   is needed.
3. Remove or revoke the Lumenfall key through its approved storage mechanism.
4. Leave generated user files alone unless their owners explicitly request
   deletion; they are normal Open WebUI files, not disposable cache entries.
5. If a database restore is genuinely required, use the established verified
   Open WebUI/Restic procedure rather than editing SQLite directly.
