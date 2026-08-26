"""Deterministic, privacy-preserving identifiers for ledger records."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


MINIMUM_KEY_BYTES = 32
_FULL_DIGEST_BYTES = hashlib.sha256().digest_size
_KEY_ID_BYTES = 16


class IdentifierError(ValueError):
    """Raised when an identifier cannot be derived safely."""


def generate_shared_key(length: int = MINIMUM_KEY_BYTES) -> bytes:
    """Generate a cryptographically random shared HMAC key."""

    if (
        isinstance(length, bool)
        or not isinstance(length, int)
        or length < MINIMUM_KEY_BYTES
    ):
        raise IdentifierError(
            f"shared keys must contain at least {MINIMUM_KEY_BYTES} bytes"
        )
    return secrets.token_bytes(length)


def project_id(key: bytes, normalized_remote: str) -> str:
    """Return the stable project ID for a normalized network remote."""

    return _derive(key, "prj_h1_", "project:v1:", normalized_remote)


def thread_key(key: bytes, raw_thread_id: str) -> str:
    """Return the stable private key for a Codex thread ID."""

    return _derive(key, "thr_h1_", "thread:v1:", raw_thread_id)


def turn_key(key: bytes, raw_turn_id: str) -> str:
    """Return the stable private key for a Codex turn ID."""

    return _derive(key, "turn_h1_", "turn:v1:", raw_turn_id)


def source_event_id(key: bytes, raw_turn_id: str, ordinal: int) -> str:
    """Return the logical ID for one token checkpoint within a turn."""

    turn_id = _validate_value(raw_turn_id, "raw_turn_id")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise IdentifierError("ordinal must be a non-negative integer")
    return _derive(
        key,
        "src_h1_",
        "usage:v1:",
        f"{turn_id}:{ordinal}",
    )


def key_id(key: bytes) -> str:
    """Return a short, non-secret fingerprint used to detect key mismatch."""

    return _derive(
        key,
        "key_h1_",
        "",
        "key-id:v1",
        digest_bytes=_KEY_ID_BYTES,
    )


def _derive(
    key: bytes,
    output_prefix: str,
    domain_prefix: str,
    value: str,
    *,
    digest_bytes: int = _FULL_DIGEST_BYTES,
) -> str:
    secret = _validate_key(key)
    clean_value = _validate_value(value, "value")
    message = f"{domain_prefix}{clean_value}".encode("utf-8")
    digest = hmac.digest(secret, message, "sha256")[:digest_bytes]
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{output_prefix}{encoded}"


def _validate_key(key: bytes) -> bytes:
    if not isinstance(key, bytes):
        raise IdentifierError("key must be bytes")
    if len(key) < MINIMUM_KEY_BYTES:
        raise IdentifierError(
            f"shared keys must contain at least {MINIMUM_KEY_BYTES} bytes"
        )
    return key


def _validate_value(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise IdentifierError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise IdentifierError(f"{field_name} must be a non-empty trimmed string")
    return value
