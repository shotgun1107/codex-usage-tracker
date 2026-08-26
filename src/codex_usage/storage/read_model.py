"""Atomic population and queries for the disposable SQLite read model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3

from codex_usage.ledger.replay import ReplayResult


class ReadModelDataError(ValueError):
    """Replay output cannot be represented by the read model schema."""


@dataclass(frozen=True, slots=True)
class ReadModelState:
    generation: int
    rebuilt_at: str
    key_id: str | None
    input_event_count: int
    effective_usage_count: int


@dataclass(frozen=True, slots=True)
class ReadModelCounts:
    usage: int
    mappings: int
    aliases: int
    quota: int


@dataclass(frozen=True, slots=True)
class DailyUsageRow:
    utc_date: str
    effective_project_id: str | None
    device_id: str
    model: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    event_count: int


def replace_read_model(
    connection: sqlite3.Connection,
    replay: ReplayResult,
    *,
    generation: int,
) -> ReadModelState:
    """Replace all derived tables inside the caller's open transaction."""

    connection.execute("DELETE FROM usage_events")
    connection.execute("DELETE FROM mapping_events")
    connection.execute("DELETE FROM project_aliases")
    connection.execute("DELETE FROM quota_snapshots")

    alias_event_ids: dict[str, str] = {}
    for event in replay.mapping_events:
        connection.execute(
            """
            INSERT INTO mapping_events (
                logical_key, event_id, revision, occurred_at, kind,
                subject_type, subject_id, target_project_id,
                display_value, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _mapping_logical_key(event),
                _required_text(event, "event_id"),
                _required_int(event, "revision", minimum=1),
                _required_text(event, "occurred_at"),
                _required_text(event, "kind"),
                _required_text(event, "subject_type"),
                _required_text(event, "subject_id"),
                _optional_text(event, "target_project_id"),
                _optional_text(event, "display_value"),
                _canonical_json(event),
            ),
        )
        if event.get("kind") == "project_alias":
            alias_event_ids[_required_text(event, "subject_id")] = _required_text(
                event,
                "event_id",
            )

    for source, target in replay.project_aliases.items():
        event_id = alias_event_ids.get(source)
        if event_id is None:
            raise ReadModelDataError(
                "effective project alias has no mapping event"
            )
        connection.execute(
            """
            INSERT INTO project_aliases (
                source_project_id, target_project_id, event_id
            ) VALUES (?, ?, ?)
            """,
            (source, target, event_id),
        )

    for usage in replay.usage_events:
        _insert_usage(connection, usage.payload, usage.effective_project_id)

    for event in replay.quota_snapshots:
        connection.execute(
            """
            INSERT INTO quota_snapshots (
                event_id, occurred_at, device_id, key_id, scope_key,
                window_minutes, used_percent, remaining_percent,
                reset_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _required_text(event, "event_id"),
                _required_text(event, "occurred_at"),
                _required_text(event, "device_id"),
                _required_text(event, "key_id"),
                _optional_text(event, "scope_key"),
                _optional_int(event, "window_minutes", minimum=1),
                _optional_number(event, "used_percent"),
                _optional_number(event, "remaining_percent"),
                _optional_text(event, "reset_at"),
                _canonical_json(event),
            ),
        )

    state = ReadModelState(
        generation=generation,
        rebuilt_at=_utc_now(),
        key_id=replay.key_id,
        input_event_count=replay.input_event_count,
        effective_usage_count=len(replay.usage_events),
    )
    connection.execute(
        """
        INSERT INTO read_model_state (
            singleton, generation, rebuilt_at, key_id,
            input_event_count, effective_usage_count
        ) VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            generation = excluded.generation,
            rebuilt_at = excluded.rebuilt_at,
            key_id = excluded.key_id,
            input_event_count = excluded.input_event_count,
            effective_usage_count = excluded.effective_usage_count
        """,
        (
            state.generation,
            state.rebuilt_at,
            state.key_id,
            state.input_event_count,
            state.effective_usage_count,
        ),
    )
    return state


def load_read_model_state(connection: sqlite3.Connection) -> ReadModelState | None:
    row = connection.execute(
        """
        SELECT generation, rebuilt_at, key_id, input_event_count,
               effective_usage_count
        FROM read_model_state
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    return ReadModelState(
        generation=row["generation"],
        rebuilt_at=row["rebuilt_at"],
        key_id=row["key_id"],
        input_event_count=row["input_event_count"],
        effective_usage_count=row["effective_usage_count"],
    )


def load_read_model_counts(connection: sqlite3.Connection) -> ReadModelCounts:
    tables = (
        "usage_events",
        "mapping_events",
        "project_aliases",
        "quota_snapshots",
    )
    counts = {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }
    return ReadModelCounts(
        usage=counts["usage_events"],
        mappings=counts["mapping_events"],
        aliases=counts["project_aliases"],
        quota=counts["quota_snapshots"],
    )


def load_daily_usage_utc(connection: sqlite3.Connection) -> tuple[DailyUsageRow, ...]:
    rows = connection.execute(
        """
        SELECT utc_date, effective_project_id, device_id, model,
               input_tokens, cached_input_tokens, output_tokens,
               reasoning_output_tokens, total_tokens, event_count
        FROM usage_daily_utc
        ORDER BY utc_date, effective_project_id, device_id, model
        """
    ).fetchall()
    return tuple(
        DailyUsageRow(
            utc_date=row["utc_date"],
            effective_project_id=row["effective_project_id"],
            device_id=row["device_id"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            cached_input_tokens=row["cached_input_tokens"],
            output_tokens=row["output_tokens"],
            reasoning_output_tokens=row["reasoning_output_tokens"],
            total_tokens=row["total_tokens"],
            event_count=row["event_count"],
        )
        for row in rows
    )


def _insert_usage(
    connection: sqlite3.Connection,
    event: Mapping[str, object],
    effective_project_id: str | None,
) -> None:
    delta = _optional_object(event, "delta")
    flags = event.get("flags")
    if not isinstance(flags, list) or any(
        not isinstance(flag, str) for flag in flags
    ):
        raise ReadModelDataError("usage flags must be a string array")
    connection.execute(
        """
        INSERT INTO usage_events (
            source_event_id, event_id, revision, occurred_at,
            device_id, key_id, original_project_id,
            effective_project_id, project_resolution,
            activity_repository_count, thread_key, root_thread_key,
            parent_thread_key, forked_from_thread_key, turn_key,
            token_event_ordinal, operation, model, reasoning_effort,
            source_kind, cli_version, delta_input_tokens,
            delta_cached_input_tokens, delta_cache_write_input_tokens,
            delta_output_tokens, delta_reasoning_output_tokens,
            delta_total_tokens, flags_json, payload_json
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            _required_text(event, "source_event_id"),
            _required_text(event, "event_id"),
            _required_int(event, "revision", minimum=1),
            _required_text(event, "occurred_at"),
            _required_text(event, "device_id"),
            _required_text(event, "key_id"),
            _optional_text(event, "project_id"),
            effective_project_id,
            _required_text(event, "project_resolution"),
            _required_int(event, "activity_repository_count", minimum=0),
            _required_text(event, "thread_key"),
            _optional_text(event, "root_thread_key"),
            _optional_text(event, "parent_thread_key"),
            _optional_text(event, "forked_from_thread_key"),
            _optional_text(event, "turn_key"),
            _required_int(event, "token_event_ordinal", minimum=0),
            _required_text(event, "operation"),
            _optional_text(event, "model"),
            _optional_text(event, "reasoning_effort"),
            _required_text(event, "source_kind"),
            _optional_text(event, "cli_version"),
            _token_value(delta, "input_tokens"),
            _token_value(delta, "cached_input_tokens"),
            _token_value(delta, "cache_write_input_tokens"),
            _token_value(delta, "output_tokens"),
            _token_value(delta, "reasoning_output_tokens"),
            _token_value(delta, "total_tokens"),
            json.dumps(flags, separators=(",", ":"), ensure_ascii=False),
            _canonical_json(event),
        ),
    )


def _canonical_json(value: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ReadModelDataError(
            "read model event is not canonical JSON"
        ) from error


def _required_text(event: Mapping[str, object], field: str) -> str:
    value = event.get(field)
    if not isinstance(value, str) or not value:
        raise ReadModelDataError(f"{field} must be a non-empty string")
    return value


def _optional_text(event: Mapping[str, object], field: str) -> str | None:
    value = event.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ReadModelDataError(f"{field} must be a non-empty string or null")
    return value


def _required_int(
    event: Mapping[str, object],
    field: str,
    *,
    minimum: int,
) -> int:
    value = event.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ReadModelDataError(f"{field} must be an integer >= {minimum}")
    return value


def _optional_int(
    event: Mapping[str, object],
    field: str,
    *,
    minimum: int,
) -> int | None:
    value = event.get(field)
    if value is None:
        return None
    return _required_int(event, field, minimum=minimum)


def _optional_number(event: Mapping[str, object], field: str) -> float | int | None:
    value = event.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReadModelDataError(f"{field} must be a number or null")
    return value


def _optional_object(
    event: Mapping[str, object],
    field: str,
) -> Mapping[str, object] | None:
    value = event.get(field)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ReadModelDataError(f"{field} must be an object or null")
    return value


def _token_value(counts: Mapping[str, object] | None, field: str) -> int | None:
    if counts is None:
        return None
    value = counts.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReadModelDataError(
            f"delta.{field} must be non-negative or null"
        )
    return value


def _mapping_logical_key(event: Mapping[str, object]) -> str:
    return ":".join(
        (
            _required_text(event, "kind"),
            _required_text(event, "subject_type"),
            _required_text(event, "subject_id"),
        )
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
