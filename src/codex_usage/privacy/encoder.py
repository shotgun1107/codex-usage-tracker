"""Convert local calculated checkpoints into shareable usage events."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
import uuid
from datetime import timezone

from codex_usage.domain.attribution import ProjectAttribution
from codex_usage.domain.lineage import LineageGraph
from codex_usage.domain.token_usage import CalculatedTokenEvent, TokenCounts
from codex_usage.privacy.identifiers import (
    fallback_source_event_id,
    key_id,
    project_id,
    source_event_id,
    thread_key,
    turn_key,
    usage_event_id,
)
from codex_usage.sources.codex_jsonl import RolloutMetadata


_PROJECT_ID = re.compile(r"prj_h1_[A-Za-z0-9_-]{43}")


class UsageEncodingError(ValueError):
    """Local usage evidence cannot be encoded without ambiguity or leakage."""


class UsageEventEncoder:
    """HMAC local identifiers and drop raw project and lineage values."""

    def __init__(
        self,
        shared_key: bytes,
        device_id: str,
        *,
        parser_version: str,
    ) -> None:
        try:
            canonical_device_id = str(uuid.UUID(device_id))
        except (ValueError, AttributeError) as error:
            raise UsageEncodingError("device_id must be a UUID") from error
        if canonical_device_id != device_id:
            raise UsageEncodingError("device_id must use canonical UUID text")
        if not parser_version or parser_version != parser_version.strip():
            raise UsageEncodingError("parser_version must be non-empty and trimmed")
        self._key = shared_key
        self.device_id = canonical_device_id
        self.parser_version = parser_version
        self.key_id = key_id(shared_key)

    def source_id(self, event: CalculatedTokenEvent) -> str:
        checkpoint = event.checkpoint
        if checkpoint.turn_id is not None:
            return source_event_id(
                self._key,
                checkpoint.turn_id,
                checkpoint.token_event_ordinal,
            )
        digest = _fallback_payload_digest(event)
        return fallback_source_event_id(
            self._key,
            checkpoint.rollout_thread_id,
            "token_count",
            checkpoint.record_index,
            digest,
        )

    def encode(
        self,
        event: CalculatedTokenEvent,
        attribution: ProjectAttribution,
        metadata: RolloutMetadata,
        lineage: LineageGraph,
    ) -> dict[str, object]:
        checkpoint = event.checkpoint
        if metadata.thread_id != checkpoint.rollout_thread_id:
            raise UsageEncodingError("rollout metadata does not match checkpoint")
        if checkpoint.occurred_at.utcoffset() != timezone.utc.utcoffset(
            checkpoint.occurred_at
        ):
            raise UsageEncodingError("checkpoint time must use UTC")

        raw_parent = lineage.parent_of(metadata.thread_id)
        raw_root = lineage.root_of(metadata.thread_id)
        logical_source_id = self.source_id(event)
        revision = 1
        payload: dict[str, object] = {
            "schema_version": 1,
            "event_type": "usage_checkpoint",
            "source_event_id": logical_source_id,
            "revision": revision,
            "supersedes": None,
            "voided": False,
            "parser_version": self.parser_version,
            "device_id": self.device_id,
            "key_id": self.key_id,
            "project_id": self._project_id(attribution.project_identity),
            "project_resolution": attribution.resolution.value,
            "activity_repository_count": attribution.activity_repository_count,
            "thread_key": thread_key(self._key, metadata.thread_id),
            "root_thread_key": _optional_thread_key(self._key, raw_root),
            "parent_thread_key": _optional_thread_key(self._key, raw_parent),
            "forked_from_thread_key": _optional_thread_key(
                self._key,
                metadata.forked_from_id,
            ),
            "turn_key": _optional_turn_key(self._key, checkpoint.turn_id),
            "token_event_ordinal": checkpoint.token_event_ordinal,
            "operation": checkpoint.operation.value,
            "occurred_at": checkpoint.occurred_at.isoformat().replace(
                "+00:00",
                "Z",
            ),
            "model": checkpoint.model,
            "reasoning_effort": checkpoint.reasoning_effort,
            "source_kind": metadata.source_kind,
            "cli_version": metadata.cli_version,
            "cumulative": _counts(checkpoint.cumulative),
            "delta": _counts(event.delta) if event.delta is not None else None,
            "reported_last": (
                _counts(checkpoint.reported_last)
                if checkpoint.reported_last is not None
                else None
            ),
            "flags": list(event.flags),
        }
        payload["event_id"] = usage_event_id(
            self._key,
            logical_source_id,
            revision,
            payload,
        )
        return payload

    def _project_id(self, identity: str | None) -> str | None:
        if identity is None:
            return None
        if _PROJECT_ID.fullmatch(identity):
            return identity
        return project_id(self._key, identity)


def _optional_thread_key(key: bytes, raw_value: str | None) -> str | None:
    return thread_key(key, raw_value) if raw_value is not None else None


def _optional_turn_key(key: bytes, raw_value: str | None) -> str | None:
    return turn_key(key, raw_value) if raw_value is not None else None


def _counts(counts: TokenCounts) -> dict[str, int | None]:
    return {
        field_name: getattr(counts, field_name)
        for field_name in TokenCounts.field_names
    }


def _fallback_payload_digest(event: CalculatedTokenEvent) -> str:
    checkpoint = event.checkpoint
    value: Mapping[str, object] = {
        "occurred_at": checkpoint.occurred_at.isoformat(),
        "operation": checkpoint.operation.value,
        "cumulative": _counts(checkpoint.cumulative),
        "reported_last": (
            _counts(checkpoint.reported_last)
            if checkpoint.reported_last is not None
            else None
        ),
    }
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
