"""Timezone-aware usage aggregation over the disposable SQLite read model."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


GROUP_DIMENSIONS = (
    "project",
    "date",
    "thread",
    "model",
    "effort",
    "device",
    "source",
)
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class ReportError(ValueError):
    """A report query or local read model is invalid."""


@dataclass(frozen=True, slots=True)
class ReportQuery:
    from_date: date | None = None
    to_date: date | None = None
    timezone_name: str = "Asia/Seoul"
    project: str | None = None
    model: str | None = None
    device: str | None = None
    source: str | None = None
    group_by: tuple[str, ...] = ("project", "date")

    def __post_init__(self) -> None:
        if self.from_date is not None and self.to_date is not None:
            if self.from_date > self.to_date:
                raise ReportError("from_date must not be after to_date")
        if not self.group_by:
            raise ReportError("group_by must contain at least one dimension")
        if len(set(self.group_by)) != len(self.group_by):
            raise ReportError("group_by must not contain duplicates")
        invalid = set(self.group_by) - set(GROUP_DIMENSIONS)
        if invalid:
            raise ReportError("group_by contains an unsupported dimension")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ReportError("timezone is unavailable") from error


@dataclass(frozen=True, slots=True)
class TokenMetric:
    value: int | None
    partial: bool


@dataclass(frozen=True, slots=True)
class TokenSummary:
    input_tokens: TokenMetric
    cached_input_tokens: TokenMetric
    cache_write_input_tokens: TokenMetric
    output_tokens: TokenMetric
    reasoning_output_tokens: TokenMetric
    total_tokens: TokenMetric
    included_events: int
    excluded_events: int


@dataclass(frozen=True, slots=True)
class ReportRow:
    dimensions: Mapping[str, str]
    tokens: TokenSummary
    cumulative_total_tokens: int


@dataclass(frozen=True, slots=True)
class UsageReport:
    timezone_name: str
    from_date: date | None
    to_date: date | None
    group_by: tuple[str, ...]
    rows: tuple[ReportRow, ...]
    total: TokenSummary


@dataclass(frozen=True, slots=True)
class _UsageRecord:
    local_date: date
    project_id: str | None
    project_label: str
    device_id: str
    device_label: str
    thread_key: str
    model: str | None
    effort: str | None
    source: str
    token_values: Mapping[str, int | None]


class _Accumulator:
    def __init__(self) -> None:
        self.sums = {field: 0 for field in TOKEN_FIELDS}
        self.seen = {field: 0 for field in TOKEN_FIELDS}
        self.missing = {field: 0 for field in TOKEN_FIELDS}
        self.included_events = 0
        self.excluded_events = 0

    def add(self, record: _UsageRecord) -> None:
        if record.token_values["total_tokens"] is None:
            self.excluded_events += 1
            return
        self.included_events += 1
        for field in TOKEN_FIELDS:
            value = record.token_values[field]
            if value is None:
                self.missing[field] += 1
            else:
                self.sums[field] += value
                self.seen[field] += 1

    def summary(self) -> TokenSummary:
        metrics = {
            field: TokenMetric(
                value=self.sums[field] if self.seen[field] else None,
                partial=bool(self.missing[field] and self.seen[field]),
            )
            for field in TOKEN_FIELDS
        }
        return TokenSummary(
            **metrics,
            included_events=self.included_events,
            excluded_events=self.excluded_events,
        )


def build_usage_report(
    database_path: str | Path,
    query: ReportQuery,
) -> UsageReport:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise ReportError("local read model does not exist; run collect first")
    timezone_info = ZoneInfo(query.timezone_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        project_names, device_names = _load_names(connection)
        records = tuple(
            _record_from_row(row, timezone_info, project_names, device_names)
            for row in connection.execute(
                """
                SELECT occurred_at, effective_project_id, device_id, thread_key,
                       model, reasoning_effort, source_kind,
                       delta_input_tokens, delta_cached_input_tokens,
                       delta_cache_write_input_tokens, delta_output_tokens,
                       delta_reasoning_output_tokens, delta_total_tokens
                FROM usage_events
                """
            )
        )
    except sqlite3.Error as error:
        raise ReportError("local read model cannot be queried") from error
    finally:
        if connection is not None:
            connection.close()

    filtered = tuple(record for record in records if _matches(record, query))
    overall = _Accumulator()
    grouped: dict[tuple[str, ...], _Accumulator] = defaultdict(_Accumulator)
    labels: dict[tuple[str, ...], tuple[str, ...]] = {}
    for record in filtered:
        overall.add(record)
        raw_key, display_labels = _group_key(record, query.group_by)
        grouped[raw_key].add(record)
        labels[raw_key] = display_labels

    sorted_keys = sorted(grouped, key=lambda key: _sort_key(key, query.group_by))
    cumulative_by_series: dict[tuple[str, ...], int] = defaultdict(int)
    rows: list[ReportRow] = []
    for raw_key in sorted_keys:
        summary = grouped[raw_key].summary()
        series_key = tuple(
            value
            for dimension, value in zip(query.group_by, raw_key, strict=True)
            if dimension != "date"
        )
        row_total = summary.total_tokens.value or 0
        cumulative_by_series[series_key] += row_total
        rows.append(
            ReportRow(
                dimensions=MappingProxyType(
                    dict(zip(query.group_by, labels[raw_key], strict=True))
                ),
                tokens=summary,
                cumulative_total_tokens=cumulative_by_series[series_key],
            )
        )

    return UsageReport(
        timezone_name=query.timezone_name,
        from_date=query.from_date,
        to_date=query.to_date,
        group_by=query.group_by,
        rows=tuple(rows),
        total=overall.summary(),
    )


def _load_names(
    connection: sqlite3.Connection,
) -> tuple[dict[str, str], dict[str, str]]:
    aliases = {
        row["source_project_id"]: row["target_project_id"]
        for row in connection.execute(
            "SELECT source_project_id, target_project_id FROM project_aliases"
        )
    }
    project_names: dict[str, str] = {}
    device_names: dict[str, str] = {}
    rows = connection.execute(
        """
        SELECT occurred_at, kind, subject_id, display_value
        FROM mapping_events
        WHERE kind IN ('project_name', 'device_name')
          AND display_value IS NOT NULL
        ORDER BY occurred_at, event_id
        """
    )
    for row in rows:
        if row["kind"] == "project_name":
            project_names[_resolve_alias(row["subject_id"], aliases)] = _single_line(
                row["display_value"]
            )
        else:
            device_names[row["subject_id"]] = _single_line(row["display_value"])
    return project_names, device_names


def _record_from_row(
    row: sqlite3.Row,
    timezone_info: ZoneInfo,
    project_names: Mapping[str, str],
    device_names: Mapping[str, str],
) -> _UsageRecord:
    occurred_at = _parse_utc(row["occurred_at"])
    project_id = row["effective_project_id"]
    device_id = row["device_id"]
    return _UsageRecord(
        local_date=occurred_at.astimezone(timezone_info).date(),
        project_id=project_id,
        project_label=(
            "미분류"
            if project_id is None
            else project_names.get(project_id, _short_identifier(project_id))
        ),
        device_id=device_id,
        device_label=device_names.get(device_id, _short_identifier(device_id)),
        thread_key=row["thread_key"],
        model=row["model"],
        effort=row["reasoning_effort"],
        source=row["source_kind"],
        token_values=MappingProxyType(
            {
                field: row[f"delta_{field}"]
                for field in TOKEN_FIELDS
            }
        ),
    )


def _matches(record: _UsageRecord, query: ReportQuery) -> bool:
    if query.from_date is not None and record.local_date < query.from_date:
        return False
    if query.to_date is not None and record.local_date > query.to_date:
        return False
    if query.project is not None:
        project_values = {
            record.project_id,
            record.project_label,
            "unclassified" if record.project_id is None else None,
            "미분류" if record.project_id is None else None,
        }
        if query.project not in project_values:
            return False
    if query.model is not None and query.model != record.model:
        return False
    if query.device is not None and query.device not in {
        record.device_id,
        record.device_label,
    }:
        return False
    return query.source is None or query.source == record.source


def _group_key(
    record: _UsageRecord,
    dimensions: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw_values = {
        "project": record.project_id or "",
        "date": record.local_date.isoformat(),
        "thread": record.thread_key,
        "model": record.model or "",
        "effort": record.effort or "",
        "device": record.device_id,
        "source": record.source,
    }
    labels = {
        "project": record.project_label,
        "date": record.local_date.isoformat(),
        "thread": _short_identifier(record.thread_key),
        "model": record.model or "알 수 없음",
        "effort": record.effort or "알 수 없음",
        "device": record.device_label,
        "source": record.source,
    }
    return (
        tuple(raw_values[dimension] for dimension in dimensions),
        tuple(labels[dimension] for dimension in dimensions),
    )


def _sort_key(
    raw_key: tuple[str, ...],
    dimensions: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(
        value if dimension == "date" else value.casefold()
        for dimension, value in zip(dimensions, raw_key, strict=True)
    )


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ReportError("read model contains an invalid timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ReportError("read model contains an invalid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReportError("read model timestamp is not UTC")
    return parsed


def _resolve_alias(project_id: str, aliases: Mapping[str, str]) -> str:
    current = project_id
    visited: set[str] = set()
    while current in aliases and current not in visited:
        visited.add(current)
        current = aliases[current]
    return current


def _short_identifier(value: str) -> str:
    if "_h1_" in value:
        prefix, suffix = value.split("_h1_", 1)
        return f"{prefix}_{suffix[:8]}…"
    if len(value) > 12:
        return f"{value[:8]}…"
    return value


def _single_line(value: str) -> str:
    return "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in value
    )
