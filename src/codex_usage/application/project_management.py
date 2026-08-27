"""Project catalog and append-only manual mapping use cases."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from codex_usage.application.collect import find_codex_state_database
from codex_usage.application.lock import ApplicationLock
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerReader, LedgerWriter
from codex_usage.ledger.replay import ReplayResult, replay_ledger_events
from codex_usage.ledger.schema_validation import LedgerSchemaValidator
from codex_usage.privacy.guard import LedgerPrivacyGuard
from codex_usage.privacy.identifiers import (
    key_id,
    mapping_event_id,
    thread_key,
    turn_key,
)
from codex_usage.sources.codex_sqlite import SqliteAdapterError, load_thread_inventory
from codex_usage.storage.read_model import ReadModelState
from codex_usage.storage.sqlite import LocalStateStore


_PROJECT_ID = re.compile(r"prj_h1_[A-Za-z0-9_-]{43}")
_THREAD_KEY = re.compile(r"thr_h1_[A-Za-z0-9_-]{43}")
_TURN_KEY = re.compile(r"turn_h1_[A-Za-z0-9_-]{43}")


class ProjectManagementError(RuntimeError):
    """A project query or mapping mutation is invalid or unsafe."""


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    name: str | None
    total_tokens: int
    thread_count: int
    included_events: int
    excluded_events: int


@dataclass(frozen=True, slots=True)
class UnresolvedThread:
    thread_key: str
    local_thread_id: str | None
    resolutions: tuple[str, ...]
    total_tokens: int
    included_events: int
    excluded_events: int


@dataclass(frozen=True, slots=True)
class MappingWriteResult:
    event_id: str
    revision: int
    changed: bool
    read_model_state: ReadModelState


@dataclass(slots=True)
class _UnresolvedAccumulator:
    resolutions: set[str] = field(default_factory=set)
    total_tokens: int = 0
    included_events: int = 0
    excluded_events: int = 0


class ProjectManagementService:
    def __init__(self, config: AppConfig, shared_key: bytes) -> None:
        if key_id(shared_key) != config.key_id:
            raise ProjectManagementError("configured HMAC key does not match key_id")
        self.config = config
        self.shared_key = shared_key

    def list_projects(self) -> tuple[ProjectSummary, ...]:
        connection = _read_model_connection(self.config.state_db)
        try:
            aliases = {
                row["source_project_id"]: row["target_project_id"]
                for row in connection.execute(
                    "SELECT source_project_id, target_project_id FROM project_aliases"
                )
            }
            names: dict[str, str] = {}
            for row in connection.execute(
                """
                SELECT subject_id, display_value
                FROM mapping_events
                WHERE kind = 'project_name' AND display_value IS NOT NULL
                ORDER BY occurred_at, event_id
                """
            ):
                names[_resolve_alias(row["subject_id"], aliases)] = row["display_value"]
            rows = connection.execute(
                """
                SELECT effective_project_id AS project_id,
                       COUNT(DISTINCT thread_key) AS thread_count,
                       SUM(CASE WHEN delta_total_tokens IS NOT NULL
                                THEN delta_total_tokens ELSE 0 END) AS total_tokens,
                       SUM(CASE WHEN delta_total_tokens IS NOT NULL
                                THEN 1 ELSE 0 END) AS included_events,
                       SUM(CASE WHEN delta_total_tokens IS NULL
                                THEN 1 ELSE 0 END) AS excluded_events
                FROM usage_events
                WHERE effective_project_id IS NOT NULL
                GROUP BY effective_project_id
                ORDER BY total_tokens DESC, project_id
                """
            ).fetchall()
            return tuple(
                ProjectSummary(
                    project_id=row["project_id"],
                    name=names.get(row["project_id"]),
                    total_tokens=int(row["total_tokens"] or 0),
                    thread_count=int(row["thread_count"]),
                    included_events=int(row["included_events"] or 0),
                    excluded_events=int(row["excluded_events"] or 0),
                )
                for row in rows
            )
        except sqlite3.Error as error:
            raise ProjectManagementError("local read model cannot be queried") from error
        finally:
            connection.close()

    def list_unresolved(self) -> tuple[UnresolvedThread, ...]:
        connection = _read_model_connection(self.config.state_db)
        try:
            rows = connection.execute(
                """
                SELECT thread_key, project_resolution, delta_total_tokens
                FROM usage_events
                WHERE effective_project_id IS NULL
                ORDER BY thread_key, project_resolution, occurred_at
                """
            ).fetchall()
        except sqlite3.Error as error:
            raise ProjectManagementError("local read model cannot be queried") from error
        finally:
            connection.close()

        grouped: dict[str, _UnresolvedAccumulator] = defaultdict(
            _UnresolvedAccumulator
        )
        for row in rows:
            values = grouped[row["thread_key"]]
            values.resolutions.add(row["project_resolution"])
            total = row["delta_total_tokens"]
            if total is None:
                values.excluded_events += 1
            else:
                values.total_tokens += int(total)
                values.included_events += 1

        local_ids = self._local_thread_ids()
        return tuple(
            UnresolvedThread(
                thread_key=subject,
                local_thread_id=local_ids.get(subject),
                resolutions=tuple(sorted(values.resolutions)),
                total_tokens=values.total_tokens,
                included_events=values.included_events,
                excluded_events=values.excluded_events,
            )
            for subject, values in grouped.items()
        )

    def link(
        self,
        *,
        subject_type: str,
        subject: str,
        target_project_id: str,
    ) -> MappingWriteResult:
        if subject_type == "thread":
            subject_id = _opaque_or_hmac(
                subject,
                _THREAD_KEY,
                "thr_h1_",
                lambda value: thread_key(self.shared_key, value),
            )
        elif subject_type == "turn":
            subject_id = _opaque_or_hmac(
                subject,
                _TURN_KEY,
                "turn_h1_",
                lambda value: turn_key(self.shared_key, value),
            )
        else:
            raise ProjectManagementError("manual link subject must be thread or turn")
        project = _require_project_id(target_project_id)
        with ApplicationLock(self.config.state_db):
            events, replay, store = self._current_state()
            known_projects = _known_project_ids(replay)
            if project not in known_projects:
                raise ProjectManagementError("target project does not exist")
            project = _resolve_alias(project, replay.project_aliases)
            if not self._subject_exists(replay, subject_type, subject_id, subject):
                raise ProjectManagementError(f"{subject_type} does not exist")
            return self._write_mapping(
                events,
                replay,
                store,
                kind="manual_assignment",
                subject_type=subject_type,
                subject_id=subject_id,
                target_project_id=project,
            )

    def alias(
        self,
        *,
        source_project_id: str,
        target_project_id: str,
    ) -> MappingWriteResult:
        source = _require_project_id(source_project_id)
        target = _require_project_id(target_project_id)
        with ApplicationLock(self.config.state_db):
            events, replay, store = self._current_state()
            known_projects = _known_project_ids(replay)
            if source not in known_projects or target not in known_projects:
                raise ProjectManagementError("source and target projects must exist")
            target = _resolve_alias(target, replay.project_aliases)
            if source == target or _would_create_alias_cycle(
                source,
                target,
                replay.project_aliases,
            ):
                raise ProjectManagementError("project alias would create a cycle")
            return self._write_mapping(
                events,
                replay,
                store,
                kind="project_alias",
                subject_type="project",
                subject_id=source,
                target_project_id=target,
            )

    def _current_state(
        self,
    ) -> tuple[list[dict[str, object]], ReplayResult, LocalStateStore]:
        read = LedgerReader(self.config.ledger_root).read_all()
        if read.issues:
            raise ProjectManagementError("ledger contains a partial line")
        store = LocalStateStore(self.config.state_db)
        events = [dict(event) for event in read.events]
        events.extend(item.payload() for item in store.pending_outbox(limit=1_000_000))
        replay = replay_ledger_events(events, expected_key_id=self.config.key_id)
        if any(
            diagnostic.code == "project_alias_cycle_ignored" and diagnostic.count
            for diagnostic in replay.diagnostics
        ):
            raise ProjectManagementError("ledger already contains a project alias cycle")
        return events, replay, store

    def _write_mapping(
        self,
        events: list[dict[str, object]],
        replay: ReplayResult,
        store: LocalStateStore,
        *,
        kind: str,
        subject_type: str,
        subject_id: str,
        target_project_id: str,
    ) -> MappingWriteResult:
        existing = next(
            (
                event
                for event in replay.mapping_events
                if event.get("kind") == kind
                and event.get("subject_type") == subject_type
                and event.get("subject_id") == subject_id
            ),
            None,
        )
        existing_target = (
            existing.get("target_project_id") if existing is not None else None
        )
        if isinstance(existing_target, str) and _resolve_alias(
            existing_target,
            replay.project_aliases,
        ) == target_project_id:
            final_replay = self._flush_and_rebuild(store)
            return MappingWriteResult(
                event_id=str(existing["event_id"]),
                revision=int(existing["revision"]),
                changed=False,
                read_model_state=final_replay[1],
            )

        revision = int(existing["revision"]) + 1 if existing is not None else 1
        payload: dict[str, object] = {
            "schema_version": 1,
            "event_type": "mapping",
            "revision": revision,
            "supersedes": existing.get("event_id") if existing is not None else None,
            "device_id": self.config.device_id,
            "key_id": self.config.key_id,
            "occurred_at": _utc_now(),
            "kind": kind,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "target_project_id": target_project_id,
            "display_value": None,
        }
        logical_key = f"{kind}:{subject_type}:{subject_id}"
        payload["event_id"] = mapping_event_id(
            self.shared_key,
            logical_key,
            revision,
            payload,
        )
        LedgerSchemaValidator.default().validate(payload)
        LedgerPrivacyGuard().validate(payload)
        prospective = replay_ledger_events(
            (*events, payload),
            expected_key_id=self.config.key_id,
        )
        if any(
            diagnostic.code == "project_alias_cycle_ignored" and diagnostic.count
            for diagnostic in prospective.diagnostics
        ):
            raise ProjectManagementError("project alias would create a cycle")

        store.enqueue_outbox_events((payload,))
        _, state = self._flush_and_rebuild(store)
        return MappingWriteResult(
            event_id=str(payload["event_id"]),
            revision=revision,
            changed=True,
            read_model_state=state,
        )

    def _flush_and_rebuild(
        self,
        store: LocalStateStore,
    ) -> tuple[ReplayResult, ReadModelState]:
        writer = LedgerWriter(
            self.config.ledger_root,
            self.config.device_id,
            expected_key_id=self.config.key_id,
        )
        while True:
            result = writer.flush(store, limit=100_000)
            if result.pending_seen == 0:
                break
            if result.appended + result.already_present != result.pending_seen:
                raise ProjectManagementError("ledger writer made no complete progress")
        read = LedgerReader(self.config.ledger_root).read_all()
        if read.issues:
            raise ProjectManagementError("ledger contains a partial line")
        replay = replay_ledger_events(read.events, expected_key_id=self.config.key_id)
        return replay, store.rebuild_read_model(replay)

    def _subject_exists(
        self,
        replay: ReplayResult,
        subject_type: str,
        subject_id: str,
        raw_subject: str,
    ) -> bool:
        field = "thread_key" if subject_type == "thread" else "turn_key"
        if any(usage.payload.get(field) == subject_id for usage in replay.usage_events):
            return True
        if any(
            event.get("subject_type") == subject_type
            and event.get("subject_id") == subject_id
            for event in replay.mapping_events
        ):
            return True
        if subject_type != "thread" or _THREAD_KEY.fullmatch(raw_subject):
            return False
        database = find_codex_state_database(self.config.codex_home)
        if database is None:
            return False
        try:
            return raw_subject in load_thread_inventory(database).threads
        except SqliteAdapterError:
            return False

    def _local_thread_ids(self) -> dict[str, str]:
        database = find_codex_state_database(self.config.codex_home)
        if database is None:
            return {}
        try:
            inventory = load_thread_inventory(database)
        except SqliteAdapterError:
            return {}
        result: dict[str, str] = {}
        for raw_id in inventory.threads:
            if any(ord(character) < 32 or ord(character) == 127 for character in raw_id):
                continue
            try:
                result[thread_key(self.shared_key, raw_id)] = raw_id
            except ValueError:
                continue
        return result


def _read_model_connection(path_value: str) -> sqlite3.Connection:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
        raise ProjectManagementError("local read model does not exist; run collect first")
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as error:
        raise ProjectManagementError("local read model cannot be opened") from error


def _known_project_ids(replay: ReplayResult) -> set[str]:
    projects = set(replay.project_aliases) | set(replay.project_aliases.values())
    for usage in replay.usage_events:
        original = usage.payload.get("project_id")
        if isinstance(original, str):
            projects.add(original)
        if usage.effective_project_id is not None:
            projects.add(usage.effective_project_id)
    for event in replay.mapping_events:
        if event.get("subject_type") == "project":
            subject = event.get("subject_id")
            if isinstance(subject, str):
                projects.add(subject)
        target = event.get("target_project_id")
        if isinstance(target, str):
            projects.add(target)
    return projects


def _opaque_or_hmac(
    value: str,
    pattern: re.Pattern[str],
    prefix: str,
    encoder: Callable[[str], str],
) -> str:
    if pattern.fullmatch(value):
        return value
    if value.startswith(prefix):
        raise ProjectManagementError("pseudonymous subject ID is malformed")
    try:
        return encoder(value)
    except ValueError as error:
        raise ProjectManagementError("raw subject ID is invalid") from error


def _require_project_id(value: str) -> str:
    if not _PROJECT_ID.fullmatch(value):
        raise ProjectManagementError("project ID is invalid")
    return value


def _resolve_alias(project: str, aliases: Mapping[str, str]) -> str:
    current = project
    visited: set[str] = set()
    while current in aliases and current not in visited:
        visited.add(current)
        current = aliases[current]
    return current


def _would_create_alias_cycle(
    source: str,
    target: str,
    aliases: Mapping[str, str],
) -> bool:
    current = target
    visited: set[str] = set()
    while current in aliases and current not in visited:
        if current == source:
            return True
        visited.add(current)
        current = aliases[current]
    return current == source


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
