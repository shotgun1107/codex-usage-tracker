"""Parser for the subset of Codex rollout JSONL needed by the tracker."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import re

from codex_usage.domain.token_usage import (
    Operation,
    RawTokenCheckpoint,
    TokenCounts,
    TokenDataError,
)


class RolloutParseError(ValueError):
    """Raised when a complete rollout record is structurally invalid."""

    def __init__(self, record_index: int, message: str) -> None:
        super().__init__(f"rollout record {record_index}: {message}")
        self.record_index = record_index


@dataclass(frozen=True, slots=True)
class ParseIssue:
    """A non-fatal rollout parsing issue."""

    record_index: int
    code: str


@dataclass(frozen=True, slots=True)
class RolloutMetadata:
    """Local-only metadata from the first session_meta record."""

    thread_id: str
    root_session_id: str
    forked_from_id: str | None
    source_kind: str
    source_parent_thread_id: str | None
    cli_version: str | None
    cwd: str | None
    git_repository_url: str | None
    git_branch: str | None
    git_commit_hash: str | None


@dataclass(frozen=True, slots=True)
class RolloutParseResult:
    """Parsed metadata, token checkpoints, and non-fatal issues."""

    metadata: RolloutMetadata
    checkpoints: tuple[RawTokenCheckpoint, ...]
    issues: tuple[ParseIssue, ...]


_JS_WORKDIR = re.compile(
    r'(?m)^[ \t]*(?:workdir|cwd)[ \t]*:[ \t]*'
    r'(?P<value>"(?:\\.|[^"\\])*")'
)


def parse_rollout(lines: Iterable[str]) -> RolloutParseResult:
    """Parse complete JSONL lines from one Codex rollout.

    The caller must retain a final partial line for a later incremental read.
    Unknown record types are ignored for forward compatibility.
    """

    metadata: RolloutMetadata | None = None
    checkpoints: list[RawTokenCheckpoint] = []
    issues: list[ParseIssue] = []

    current_turn_id: str | None = None
    current_ordinal = 0
    current_operation = Operation.UNKNOWN
    current_model: str | None = None
    current_effort: str | None = None
    activity_by_turn: dict[str, set[str]] = {}

    for record_index, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise RolloutParseError(record_index, "invalid JSON") from error
        if not isinstance(record, Mapping):
            raise RolloutParseError(record_index, "record must be an object")

        outer_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}

        if outer_type == "session_meta":
            if metadata is None:
                metadata = _parse_metadata(payload, record_index)
            continue

        if outer_type == "turn_context":
            turn_id = _optional_string(payload.get("turn_id"))
            if turn_id is not None and turn_id != current_turn_id:
                current_turn_id = turn_id
                current_ordinal = 0
                current_operation = Operation.TURN
            current_model = _optional_string(payload.get("model"))
            current_effort = _optional_string(
                payload.get("effort", payload.get("reasoning_effort"))
            )
            continue

        activity_workdirs = _extract_activity_workdirs(outer_type, payload)
        if current_turn_id is not None and activity_workdirs:
            activity_by_turn.setdefault(current_turn_id, set()).update(
                activity_workdirs
            )

        if outer_type == "compacted":
            current_operation = Operation.COMPACT
            continue

        if outer_type != "event_msg":
            continue

        event_type = payload.get("type")
        if event_type == "task_started":
            current_turn_id = _optional_string(payload.get("turn_id"))
            current_ordinal = 0
            current_operation = Operation.TURN
            current_model = None
            current_effort = None
            continue

        if event_type == "task_complete":
            current_turn_id = None
            current_ordinal = 0
            current_operation = Operation.UNKNOWN
            current_model = None
            current_effort = None
            continue

        if event_type != "token_count":
            continue

        if metadata is None:
            raise RolloutParseError(
                record_index,
                "token_count appeared before the first session_meta",
            )

        info = payload.get("info")
        if not isinstance(info, Mapping):
            issues.append(ParseIssue(record_index, "token_count_without_info"))
            continue
        cumulative_values = info.get("total_token_usage")
        if not isinstance(cumulative_values, Mapping):
            issues.append(ParseIssue(record_index, "token_count_without_total_usage"))
            continue

        try:
            cumulative = TokenCounts.from_mapping(cumulative_values)
            if cumulative.total_tokens is None:
                raise TokenDataError("cumulative total_tokens is required")
            reported_values = info.get("last_token_usage")
            reported_last = (
                TokenCounts.from_mapping(reported_values)
                if isinstance(reported_values, Mapping)
                else None
            )
            occurred_at = _parse_timestamp(record.get("timestamp"), record_index)
        except TokenDataError as error:
            raise RolloutParseError(record_index, str(error)) from error

        if current_turn_id is None:
            issues.append(ParseIssue(record_index, "token_count_without_turn"))
            ordinal = 0
        else:
            ordinal = current_ordinal
            current_ordinal += 1

        checkpoints.append(
            RawTokenCheckpoint(
                rollout_thread_id=metadata.thread_id,
                rollout_forked_from_id=metadata.forked_from_id,
                turn_id=current_turn_id,
                token_event_ordinal=ordinal,
                record_index=record_index,
                occurred_at=occurred_at,
                operation=current_operation,
                model=current_model,
                reasoning_effort=current_effort,
                cumulative=cumulative,
                reported_last=reported_last,
            )
        )

    if metadata is None:
        raise RolloutParseError(1, "rollout does not contain session_meta")
    checkpoints = [
        replace(
            checkpoint,
            activity_workdirs=tuple(
                sorted(activity_by_turn.get(checkpoint.turn_id, ()))
            ),
        )
        for checkpoint in checkpoints
    ]
    return RolloutParseResult(
        metadata=metadata,
        checkpoints=tuple(checkpoints),
        issues=tuple(issues),
    )


def _parse_metadata(
    payload: Mapping[str, object],
    record_index: int,
) -> RolloutMetadata:
    thread_id = _required_string(payload.get("id"), record_index, "session_meta.id")
    root_session_id = _optional_string(payload.get("session_id")) or thread_id
    source_kind, source_parent_thread_id = _normalize_source(payload.get("source"))

    git = payload.get("git")
    if not isinstance(git, Mapping):
        git = {}

    return RolloutMetadata(
        thread_id=thread_id,
        root_session_id=root_session_id,
        forked_from_id=_optional_string(payload.get("forked_from_id")),
        source_kind=source_kind,
        source_parent_thread_id=source_parent_thread_id,
        cli_version=_optional_string(payload.get("cli_version")),
        cwd=_optional_string(payload.get("cwd")),
        git_repository_url=_optional_string(git.get("repository_url")),
        git_branch=_optional_string(git.get("branch")),
        git_commit_hash=_optional_string(git.get("commit_hash")),
    )


def _normalize_source(source: object) -> tuple[str, str | None]:
    if isinstance(source, str):
        known = {"cli", "vscode", "exec", "appServer"}
        return (source if source in known else "unknown", None)

    if not isinstance(source, Mapping):
        return ("unknown", None)
    subagent = source.get("subagent")
    if not isinstance(subagent, Mapping):
        return ("unknown", None)

    source_types = (
        ("thread_spawn", "subAgentThreadSpawn"),
        ("review", "subAgentReview"),
        ("compact", "subAgentCompact"),
        ("other", "subAgentOther"),
    )
    for raw_name, normalized_name in source_types:
        details = subagent.get(raw_name)
        if details is None:
            continue
        parent_id = (
            _optional_string(details.get("parent_thread_id"))
            if isinstance(details, Mapping)
            else None
        )
        return (normalized_name, parent_id)
    return ("subAgent", None)


def _extract_activity_workdirs(
    outer_type: object,
    payload: Mapping[str, object],
) -> tuple[str, ...]:
    discovered: set[str] = set()

    if outer_type == "response_item":
        payload_type = payload.get("type")
        if payload_type in {"custom_tool_call", "function_call"}:
            raw_input = payload.get("input", payload.get("arguments"))
            discovered.update(_workdirs_from_tool_input(raw_input))
        if payload_type in {"commandExecution", "command_execution"}:
            cwd = _optional_string(payload.get("cwd"))
            if cwd is not None:
                discovered.add(cwd)

    if outer_type == "event_msg" and payload.get("type") in {
        "item_started",
        "item_completed",
    }:
        item = payload.get("item")
        if isinstance(item, Mapping) and item.get("type") in {
            "commandExecution",
            "command_execution",
        }:
            cwd = _optional_string(item.get("cwd"))
            if cwd is not None:
                discovered.add(cwd)

    return tuple(sorted(discovered))


def _workdirs_from_tool_input(raw_input: object) -> tuple[str, ...]:
    if isinstance(raw_input, Mapping):
        discovered: set[str] = set()
        _collect_json_workdirs(raw_input, discovered)
        return tuple(sorted(discovered))
    if not isinstance(raw_input, str) or not raw_input:
        return ()

    try:
        decoded = json.loads(raw_input)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, Mapping):
        discovered = set()
        _collect_json_workdirs(decoded, discovered)
        return tuple(sorted(discovered))

    discovered = set()
    for match in _JS_WORKDIR.finditer(raw_input):
        try:
            value = json.loads(match.group("value"))
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value:
            discovered.add(value)
    return tuple(sorted(discovered))


def _collect_json_workdirs(
    value: Mapping[str, object],
    discovered: set[str],
) -> None:
    for key, child in value.items():
        if key in {"workdir", "cwd"} and isinstance(child, str) and child:
            discovered.add(child)
        elif isinstance(child, Mapping):
            _collect_json_workdirs(child, discovered)
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, Mapping):
                    _collect_json_workdirs(item, discovered)


def _parse_timestamp(value: object, record_index: int) -> datetime:
    if not isinstance(value, str) or not value:
        raise RolloutParseError(record_index, "token_count timestamp is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RolloutParseError(record_index, "token_count timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise RolloutParseError(record_index, "token_count timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _required_string(
    value: object,
    record_index: int,
    field_name: str,
) -> str:
    result = _optional_string(value)
    if result is None:
        raise RolloutParseError(record_index, f"{field_name} is missing")
    return result


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
