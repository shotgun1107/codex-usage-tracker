"""Valid sanitized ledger events shared by validation and I/O tests."""

from __future__ import annotations

import base64
import hashlib


DEVICE_ID = "00000000-0000-4000-8000-000000000001"


def opaque(prefix: str, label: str, *, digest_bytes: int = 32) -> str:
    digest = hashlib.sha256(label.encode("utf-8")).digest()[:digest_bytes]
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"{prefix}{encoded}"


KEY_ID = opaque("key_h1_", "key", digest_bytes=16)
PROJECT_ORIGINAL = opaque("prj_h1_", "project-original")
PROJECT_MANUAL = opaque("prj_h1_", "project-manual")
THREAD_ONE = opaque("thr_h1_", "thread-one")
TURN_ONE = opaque("turn_h1_", "turn-one")


def token_counts(total: int = 12) -> dict[str, int | None]:
    return {
        "input_tokens": total - 2,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": None,
        "output_tokens": 2,
        "reasoning_output_tokens": 1,
        "total_tokens": total,
    }


def usage_event(
    *,
    event_id: str = opaque("evt_h1_", "usage-one"),
    source_event_id: str = opaque("src_h1_", "source-one"),
    device_id: str = DEVICE_ID,
    project_id: str | None = PROJECT_ORIGINAL,
    thread_key: str = THREAD_ONE,
    turn_key: str | None = TURN_ONE,
    occurred_at: str = "2026-08-26T05:00:00Z",
    model: str | None = "gpt-test-model",
    total: int = 12,
) -> dict[str, object]:
    counts = token_counts(total)
    return {
        "schema_version": 1,
        "event_type": "usage_checkpoint",
        "event_id": event_id,
        "source_event_id": source_event_id,
        "revision": 1,
        "supersedes": None,
        "voided": False,
        "parser_version": "0.1.0",
        "device_id": device_id,
        "key_id": KEY_ID,
        "project_id": project_id,
        "project_resolution": "self_origin",
        "activity_repository_count": 0,
        "thread_key": thread_key,
        "root_thread_key": thread_key,
        "parent_thread_key": None,
        "forked_from_thread_key": None,
        "turn_key": turn_key,
        "token_event_ordinal": 0,
        "operation": "turn",
        "occurred_at": occurred_at,
        "model": model,
        "reasoning_effort": "medium",
        "source_kind": "vscode",
        "cli_version": "0.150.0",
        "cumulative": counts,
        "delta": counts,
        "reported_last": counts,
        "flags": [],
    }


def mapping_event(
    *,
    event_id: str = opaque("map_h1_", "mapping-one"),
    device_id: str = DEVICE_ID,
    kind: str = "manual_assignment",
    subject_type: str = "thread",
    subject_id: str = THREAD_ONE,
    target_project_id: str | None = PROJECT_MANUAL,
    occurred_at: str = "2026-08-26T06:00:00Z",
    display_value: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "mapping",
        "event_id": event_id,
        "revision": 1,
        "supersedes": None,
        "device_id": device_id,
        "key_id": KEY_ID,
        "occurred_at": occurred_at,
        "kind": kind,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "target_project_id": target_project_id,
        "display_value": display_value,
    }


def quota_event(
    *,
    event_id: str = opaque("quota_h1_", "snapshot-one"),
    device_id: str = DEVICE_ID,
    occurred_at: str = "2026-08-26T07:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "quota_snapshot",
        "event_id": event_id,
        "device_id": device_id,
        "key_id": KEY_ID,
        "occurred_at": occurred_at,
        "scope_key": opaque("account_h1_", "account"),
        "window_minutes": 300,
        "used_percent": 20.0,
        "remaining_percent": 80.0,
        "reset_at": "2026-08-26T12:00:00Z",
    }
