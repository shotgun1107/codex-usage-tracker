"""Lifecycle-aware token delta calculation and fork-history deduplication."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from codex_usage.domain.token_usage import (
    CalculatedTokenEvent,
    Operation,
    RawTokenCheckpoint,
    TokenCounts,
)


class DuplicateCheckpointConflict(ValueError):
    """Raised when one logical token event has conflicting values."""


def calculate_deltas(
    checkpoints: Iterable[RawTokenCheckpoint],
    *,
    previous_cumulative: TokenCounts | None = None,
) -> list[CalculatedTokenEvent]:
    """Calculate deltas for checkpoints from one rollout counter stream."""

    previous = previous_cumulative
    calculated: list[CalculatedTokenEvent] = []

    for checkpoint in checkpoints:
        flags: list[str] = []
        cumulative = checkpoint.cumulative

        if checkpoint.turn_id is None:
            flags.append("weak_dedupe_key")

        if cumulative.is_total_consistent() is False:
            flags.append("token_total_mismatch")

        if previous is None:
            delta = cumulative
        elif _total_regressed(cumulative, previous):
            delta = None
            flags.append("counter_regression")
        else:
            delta, component_flags = _subtract(cumulative, previous)
            flags.extend(component_flags)

        if delta is not None and delta.is_total_consistent() is False:
            flags.append("delta_total_mismatch")

        reported_total = (
            checkpoint.reported_last.total_tokens
            if checkpoint.reported_last is not None
            else None
        )
        delta_total = delta.total_tokens if delta is not None else None

        if (
            checkpoint.operation is Operation.COMPACT
            and delta_total == 0
            and reported_total is not None
            and reported_total > 0
        ):
            flags.append("compact_reported_last_excluded")
        elif (
            checkpoint.operation is not Operation.COMPACT
            and delta_total is not None
            and delta_total > 0
            and reported_total is not None
            and reported_total != delta_total
        ):
            flags.append("reported_last_mismatch")

        calculated.append(
            CalculatedTokenEvent(
                checkpoint=checkpoint,
                delta=delta,
                flags=tuple(sorted(set(flags))),
            )
        )
        previous = cumulative

    return calculated


def deduplicate_events(
    events: Iterable[CalculatedTokenEvent],
) -> list[CalculatedTokenEvent]:
    """Remove fork-copied events using turn + ordinal logical keys.

    If both the original rollout and a fork copy are present, the event from the
    shallowest fork depth is retained. Conflicting copies fail closed.
    """

    items = list(events)
    parent_by_thread = {
        event.checkpoint.rollout_thread_id: event.checkpoint.rollout_forked_from_id
        for event in items
    }
    depths = _fork_depths(parent_by_thread)
    selected: dict[tuple[str, str, int], CalculatedTokenEvent] = {}

    for event in items:
        key = event.logical_key
        existing = selected.get(key)
        if existing is None:
            selected[key] = event
            continue

        if _semantic_value(existing) != _semantic_value(event):
            raise DuplicateCheckpointConflict(
                f"conflicting token checkpoint for logical key {key!r}"
            )

        existing_rank = _origin_rank(existing, depths)
        candidate_rank = _origin_rank(event, depths)
        preferred = event if candidate_rank < existing_rank else existing
        selected[key] = _merge_optional_metadata(existing, event, preferred)

    return sorted(
        selected.values(),
        key=lambda event: (
            event.checkpoint.occurred_at,
            event.logical_key,
            event.checkpoint.rollout_thread_id,
        ),
    )


def _total_regressed(current: TokenCounts, previous: TokenCounts) -> bool:
    return (
        current.total_tokens is not None
        and previous.total_tokens is not None
        and current.total_tokens < previous.total_tokens
    )


def _subtract(
    current: TokenCounts,
    previous: TokenCounts,
) -> tuple[TokenCounts, list[str]]:
    values: dict[str, int | None] = {}
    flags: list[str] = []

    for field_name in TokenCounts.field_names:
        current_value = getattr(current, field_name)
        previous_value = getattr(previous, field_name)
        if current_value is None or previous_value is None:
            values[field_name] = None
        elif current_value < previous_value:
            values[field_name] = None
            flags.append(f"component_regression:{field_name}")
        else:
            values[field_name] = current_value - previous_value

    return TokenCounts(**values), flags


def _semantic_value(event: CalculatedTokenEvent) -> tuple[object, ...]:
    checkpoint = event.checkpoint
    return (
        checkpoint.turn_id,
        checkpoint.token_event_ordinal,
        checkpoint.operation,
        checkpoint.cumulative,
        checkpoint.reported_last,
        event.delta,
        tuple(
            flag
            for flag in event.flags
            if not flag.startswith("metadata_conflict:")
        ),
    )


def _merge_optional_metadata(
    first: CalculatedTokenEvent,
    second: CalculatedTokenEvent,
    preferred: CalculatedTokenEvent,
) -> CalculatedTokenEvent:
    model, model_conflict = _merge_value(
        first.checkpoint.model,
        second.checkpoint.model,
        conflict_already_present=(
            "metadata_conflict:model" in first.flags
            or "metadata_conflict:model" in second.flags
        ),
    )
    effort, effort_conflict = _merge_value(
        first.checkpoint.reasoning_effort,
        second.checkpoint.reasoning_effort,
        conflict_already_present=(
            "metadata_conflict:reasoning_effort" in first.flags
            or "metadata_conflict:reasoning_effort" in second.flags
        ),
    )

    flags = set(preferred.flags)
    if model_conflict:
        flags.add("metadata_conflict:model")
    if effort_conflict:
        flags.add("metadata_conflict:reasoning_effort")

    return replace(
        preferred,
        checkpoint=replace(
            preferred.checkpoint,
            model=model,
            reasoning_effort=effort,
        ),
        flags=tuple(sorted(flags)),
    )


def _merge_value(
    first: str | None,
    second: str | None,
    *,
    conflict_already_present: bool,
) -> tuple[str | None, bool]:
    if conflict_already_present:
        return (None, True)
    values = {value for value in (first, second) if value is not None}
    if len(values) > 1:
        return (None, True)
    return (next(iter(values), None), False)


def _origin_rank(
    event: CalculatedTokenEvent,
    depths: dict[str, int],
) -> tuple[int, str]:
    thread_id = event.checkpoint.rollout_thread_id
    return (depths.get(thread_id, 0), thread_id)


def _fork_depths(parent_by_thread: dict[str, str | None]) -> dict[str, int]:
    depths: dict[str, int] = {}

    def depth(thread_id: str, visiting: set[str]) -> int:
        if thread_id in depths:
            return depths[thread_id]
        if thread_id in visiting:
            raise DuplicateCheckpointConflict("fork ancestry contains a cycle")

        parent = parent_by_thread.get(thread_id)
        if parent is None or parent not in parent_by_thread:
            result = 0
        else:
            result = depth(parent, visiting | {thread_id}) + 1
        depths[thread_id] = result
        return result

    for thread_id in parent_by_thread:
        depth(thread_id, set())
    return depths
