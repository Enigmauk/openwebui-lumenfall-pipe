"""Worker authentication, fingerprints, and authenticated encryption."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretBox:
    def __init__(self, key: bytes):
        if len(key) not in {16, 24, 32}:
            raise ValueError("Worker encryption keys must be 16, 24, or 32 bytes.")
        self._cipher = AESGCM(key)

    def encrypt(self, plaintext: bytes, *, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self._cipher.encrypt(nonce, plaintext, context.encode())

    def decrypt(self, ciphertext: bytes, *, context: str) -> bytes:
        if len(ciphertext) < 29:
            raise ValueError("Encrypted worker data is incomplete.")
        return self._cipher.decrypt(
            ciphertext[:12], ciphertext[12:], context.encode()
        )


def canonical_request(model_id: str, prompt: str, options: dict) -> bytes:
    return json.dumps(
        {"model_id": model_id, "prompt": prompt, "options": options},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def request_fingerprint(key: bytes, request: bytes) -> str:
    return hmac.new(key, request, hashlib.sha256).hexdigest()


def new_idempotency_key() -> str:
    return f"lumenfall-video:{uuid.uuid4()}"


def valid_worker_bearer(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and secrets.compare_digest(candidate, expected)
