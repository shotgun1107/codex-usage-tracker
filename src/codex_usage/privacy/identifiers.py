"""Deterministic, privacy-preserving identifiers for ledger records."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping


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


def fallback_source_event_id(
    key: bytes,
    raw_thread_id: str,
    record_kind: str,
    record_ordinal: int,
    payload_digest: str,
) -> str:
    """Return a weak fallback ID for legacy records without turn IDs."""

    thread_id = _validate_value(raw_thread_id, "raw_thread_id")
    kind = _validate_value(record_kind, "record_kind")
    digest = _validate_value(payload_digest, "payload_digest")
    if (
        isinstance(record_ordinal, bool)
        or not isinstance(record_ordinal, int)
        or record_ordinal < 0
    ):
        raise IdentifierError("record_ordinal must be a non-negative integer")
    return _derive(
        key,
        "src_h1_",
        "usage-fallback:v1:",
        f"{thread_id}:{kind}:{record_ordinal}:{digest}",
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


def usage_event_id(
    key: bytes,
    logical_source_event_id: str,
    revision: int,
    payload_without_event_id: Mapping[str, object],
) -> str:
    """Bind one immutable usage revision to its complete sanitized payload."""

    source_id = _validate_value(logical_source_event_id, "logical_source_event_id")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise IdentifierError("revision must be a positive integer")
    if "event_id" in payload_without_event_id:
        raise IdentifierError("payload_without_event_id must not contain event_id")
    try:
        canonical = json.dumps(
            dict(payload_without_event_id),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise IdentifierError("usage event payload must be canonical JSON") from error
    return _derive(
        key,
        "evt_h1_",
        "event:v1:",
        f"{source_id}:{revision}:{canonical}",
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
