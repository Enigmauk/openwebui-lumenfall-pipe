# Installation, update, and rollback

## Production installation

1. Take and verify the established Open WebUI pre-change backup.
2. Create the protected host secret file without exposing its contents.
3. Mount only that file, read-only, at
   `/run/secrets/lumenfall-api-key`; validate Compose and recreate only Open
   WebUI.
4. In Open WebUI Admin → Functions, create a new Function from the reviewed
   `lumenfall_pipe.py` at a stable Python-identifier ID such as
   `lumenfall_media`.
5. Leave it disabled while reviewing `MODEL_LIST`, size, timeout, and media
   limit.
6. Verify imported source hash, absence of a key Valve, selector entries,
   task suppression, missing-key failure, and a mocked persistence cycle.
7. Enable the Function and run documented authenticated dry runs only.
8. Stop before paid generation; the first paid request is a separate Stage 3
   decision and should use a sub-cent model.

Lumenfall account recovery uses GitHub sign-in/OAuth. Never record the OAuth
credential, API key, cookies, session data, or authentication URLs.

## Update

Review the diff and `CHANGELOG.md`, run all tests against the current pinned
image, back up Open WebUI, replace the Function source while disabled, inspect
server logs, re-enable, and repeat persistence/reload checks. Never combine a
Pipe update with an Open WebUI upgrade unless both rollback paths are explicit.

## Rollback

1. Disable the Function.
2. Restore the previously exported/reviewed Function source if rollback of code
   is needed.
3. Revoke the key at Lumenfall and remove the exact host secret file if secret
   rollback is required; removing the Compose mount requires a validated
   Open WebUI-only recreate.
4. Leave generated user files alone unless their owners explicitly request
   deletion; they are normal Open WebUI files, not disposable cache entries.
5. If a database restore is genuinely required, use the established verified
   Open WebUI/Restic procedure rather than editing SQLite directly.
