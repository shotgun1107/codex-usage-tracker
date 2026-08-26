"""Deterministic replay of sanitized append-only ledger events."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import MappingProxyType
from typing import Callable


class LedgerReplayError(ValueError):
    """Base error for invalid or conflicting ledger history."""


class LedgerEventConflict(LedgerReplayError):
    """An event or revision identity was reused with different content."""


class RevisionChainError(LedgerReplayError):
    """A revision does not form an intact supersedes chain."""


class LedgerKeyMismatch(LedgerReplayError):
    """Events from different HMAC identity spaces were mixed."""


@dataclass(frozen=True, slots=True)
class EffectiveUsageEvent:
    payload: Mapping[str, object]
    effective_project_id: str | None


@dataclass(frozen=True, slots=True)
class ReplayDiagnostic:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """The effective, order-independent state derived from a whole ledger."""

    key_id: str | None
    input_event_count: int
    usage_events: tuple[EffectiveUsageEvent, ...]
    mapping_events: tuple[Mapping[str, object], ...]
    quota_snapshots: tuple[Mapping[str, object], ...]
    project_aliases: Mapping[str, str]
    diagnostics: tuple[ReplayDiagnostic, ...]


def replay_ledger_events(
    events: Iterable[Mapping[str, object]],
    *,
    expected_key_id: str | None = None,
) -> ReplayResult:
    """Replay complete ledger events regardless of file or input order."""

    canonical_by_id: dict[str, tuple[str, dict[str, object]]] = {}
    key_ids: set[str] = set()

    for event in events:
        normalized, canonical = _normalize_event(event)
        event_id = _required_string(normalized, "event_id")
        key_id = _required_string(normalized, "key_id")
        key_ids.add(key_id)
        previous = canonical_by_id.get(event_id)
        if previous is not None:
            if previous[0] != canonical:
                raise LedgerEventConflict(
                    "event_id was reused with different ledger content"
                )
            continue
        canonical_by_id[event_id] = (canonical, normalized)

    if expected_key_id is not None and (
        not isinstance(expected_key_id, str) or not expected_key_id
    ):
        raise ValueError("expected_key_id must not be empty")
    if len(key_ids) > 1:
        raise LedgerKeyMismatch("ledger contains multiple key_id values")
    key_id = next(iter(key_ids), expected_key_id)
    if expected_key_id is not None and key_id != expected_key_id:
        raise LedgerKeyMismatch("ledger key_id does not match the local key")

    unique_events = [item[1] for item in canonical_by_id.values()]
    usage_history = [
        event for event in unique_events if event["event_type"] == "usage_checkpoint"
    ]
    mapping_history = [
        event for event in unique_events if event["event_type"] == "mapping"
    ]
    quota_history = [
        event for event in unique_events if event["event_type"] == "quota_snapshot"
    ]

    latest_usage = _latest_revisions(
        usage_history,
        logical_key=lambda event: _required_string(event, "source_event_id"),
    )
    latest_mappings = _latest_revisions(
        mapping_history,
        logical_key=_mapping_logical_key,
    )

    active_usage = [
        event
        for event in latest_usage
        if _required_bool(event, "voided") is False
    ]
    aliases, alias_cycle_count = _build_aliases(latest_mappings)
    turn_manual, thread_manual = _build_manual_assignments(latest_mappings)

    effective_usage: list[EffectiveUsageEvent] = []
    for event in active_usage:
        turn_key = _optional_string(event, "turn_key")
        thread_key = _required_string(event, "thread_key")
        original_project = _optional_string(event, "project_id")
        manual_project = (
            turn_manual.get(turn_key) if turn_key is not None else None
        )
        if manual_project is None:
            manual_project = thread_manual.get(thread_key)
        project = manual_project if manual_project is not None else original_project
        effective_usage.append(
            EffectiveUsageEvent(
                payload=MappingProxyType(event),
                effective_project_id=_resolve_alias(project, aliases),
            )
        )

    effective_usage.sort(
        key=lambda item: (
            _utc_timestamp(item.payload, "occurred_at"),
            _required_string(item.payload, "event_id"),
        )
    )
    latest_mappings.sort(
        key=lambda event: (
            _utc_timestamp(event, "occurred_at"),
            _required_string(event, "event_id"),
        )
    )
    quota_history.sort(
        key=lambda event: (
            _utc_timestamp(event, "occurred_at"),
            _required_string(event, "event_id"),
        )
    )

    diagnostics = (
        (ReplayDiagnostic("project_alias_cycle_ignored", alias_cycle_count),)
        if alias_cycle_count
        else ()
    )
    return ReplayResult(
        key_id=key_id,
        input_event_count=len(unique_events),
        usage_events=tuple(effective_usage),
        mapping_events=tuple(MappingProxyType(event) for event in latest_mappings),
        quota_snapshots=tuple(MappingProxyType(event) for event in quota_history),
        project_aliases=MappingProxyType(aliases),
        diagnostics=diagnostics,
    )


def _normalize_event(
    event: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    if not isinstance(event, Mapping):
        raise LedgerReplayError("ledger event must be an object")
    try:
        canonical = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = json.loads(canonical)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LedgerReplayError("ledger event is not canonical JSON") from error
    if not isinstance(normalized, dict):
        raise LedgerReplayError("ledger event must be an object")
    if normalized.get("schema_version") != 1:
        raise LedgerReplayError("unsupported ledger schema_version")
    if normalized.get("event_type") not in {
        "usage_checkpoint",
        "mapping",
        "quota_snapshot",
    }:
        raise LedgerReplayError("unsupported ledger event_type")
    _required_string(normalized, "event_id")
    _required_string(normalized, "key_id")
    _utc_timestamp(normalized, "occurred_at")
    return normalized, canonical


def _latest_revisions(
    events: Iterable[dict[str, object]],
    *,
    logical_key: Callable[[Mapping[str, object]], str],
) -> list[dict[str, object]]:
    grouped: dict[str, dict[int, dict[str, object]]] = defaultdict(dict)
    for event in events:
        key = logical_key(event)
        revision = _required_positive_int(event, "revision")
        previous = grouped[key].get(revision)
        if previous is not None and previous != event:
            raise LedgerEventConflict(
                "one logical event revision has conflicting content"
            )
        grouped[key][revision] = event

    latest: list[dict[str, object]] = []
    for revisions in grouped.values():
        ordered_revisions = sorted(revisions)
        if ordered_revisions != list(range(1, ordered_revisions[-1] + 1)):
            raise RevisionChainError("ledger revision history contains a gap")
        for revision in ordered_revisions:
            event = revisions[revision]
            supersedes = _optional_string(event, "supersedes")
            if revision == 1:
                if supersedes is not None:
                    raise RevisionChainError(
                        "first revision must not supersede another event"
                    )
            elif supersedes != _required_string(revisions[revision - 1], "event_id"):
                raise RevisionChainError(
                    "revision does not supersede the preceding event"
                )
        latest.append(revisions[ordered_revisions[-1]])
    return latest


def _mapping_logical_key(event: Mapping[str, object]) -> str:
    return ":".join(
        (
            _required_string(event, "kind"),
            _required_string(event, "subject_type"),
            _required_string(event, "subject_id"),
        )
    )


def _build_aliases(
    mappings: Iterable[Mapping[str, object]],
) -> tuple[dict[str, str], int]:
    edges: dict[str, str] = {}
    for event in mappings:
        if event.get("kind") != "project_alias":
            continue
        if event.get("subject_type") != "project":
            raise LedgerReplayError("project_alias subject_type must be project")
        source = _required_string(event, "subject_id")
        target = _optional_string(event, "target_project_id")
        if target is not None:
            edges[source] = target

    cycle_nodes = _alias_cycle_nodes(edges)
    active_edges = {
        source: target
        for source, target in edges.items()
        if source not in cycle_nodes
    }
    resolved = {
        source: _resolve_alias(target, active_edges) or target
        for source, target in active_edges.items()
    }
    return resolved, len(cycle_nodes)


def _alias_cycle_nodes(edges: Mapping[str, str]) -> set[str]:
    cycles: set[str] = set()
    finished: set[str] = set()
    for start in edges:
        if start in finished:
            continue
        path: list[str] = []
        positions: dict[str, int] = {}
        current = start
        while current in edges and current not in finished:
            if current in positions:
                cycles.update(path[positions[current] :])
                break
            positions[current] = len(path)
            path.append(current)
            current = edges[current]
        finished.update(path)
    return cycles


def _build_manual_assignments(
    mappings: Iterable[Mapping[str, object]],
) -> tuple[dict[str, str], dict[str, str]]:
    turns: dict[str, str] = {}
    threads: dict[str, str] = {}
    for event in mappings:
        if event.get("kind") != "manual_assignment":
            continue
        subject_type = _required_string(event, "subject_type")
        subject_id = _required_string(event, "subject_id")
        target = _optional_string(event, "target_project_id")
        destination = turns if subject_type == "turn" else threads
        if subject_type not in {"turn", "thread"}:
            raise LedgerReplayError(
                "manual_assignment subject_type must be turn or thread"
            )
        if target is not None:
            destination[subject_id] = target
    return turns, threads


def _resolve_alias(project_id: str | None, aliases: Mapping[str, str]) -> str | None:
    if project_id is None:
        return None
    current = project_id
    visited: set[str] = set()
    while current in aliases and current not in visited:
        visited.add(current)
        current = aliases[current]
    return current


def _required_string(event: Mapping[str, object], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise LedgerReplayError(f"{field} must be a non-empty string")
    return value


def _optional_string(event: Mapping[str, object], field: str) -> str | None:
    value = event.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise LedgerReplayError(f"{field} must be a non-empty string or null")
    return value


def _required_positive_int(event: Mapping[str, object], field: str) -> int:
    value = event.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerReplayError(f"{field} must be a positive integer")
    return value


def _required_bool(event: Mapping[str, object], field: str) -> bool:
    value = event.get(field)
    if not isinstance(value, bool):
        raise LedgerReplayError(f"{field} must be a boolean")
    return value


def _utc_timestamp(event: Mapping[str, object], field: str) -> datetime:
    value = _required_string(event, field)
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise LedgerReplayError(f"{field} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LedgerReplayError(f"{field} must use UTC")
    return parsed
