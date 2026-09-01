"""Read-only diagnostics for a configured Codex Usage Tracker device."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path
import sqlite3

from codex_usage.application.collect import find_codex_state_database
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerIoError, LedgerReader
from codex_usage.ledger.replay import (
    LedgerReplayError,
    ReplayResult,
    replay_ledger_events,
)
from codex_usage.ledger.schema_validation import LedgerSchemaError
from codex_usage.privacy.guard import PrivacyViolation
from codex_usage.privacy.identifiers import key_id
from codex_usage.sources.codex_jsonl import RolloutParseError, parse_rollout
from codex_usage.sources.codex_sqlite import (
    SqliteAdapterError,
    ThreadInventory,
    load_thread_inventory,
)
from codex_usage.sources.rollout_files import discover_rollout_files
from codex_usage.storage.schema import DATABASE_VERSION
from codex_usage.sync.git import GitLedgerRepository, GitSyncError


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class DoctorResult:
    checks: tuple[DoctorCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status is CheckStatus.ERROR for check in self.checks)


@dataclass(frozen=True, slots=True)
class _RolloutInventory:
    thread_ids: frozenset[str]
    cli_versions: tuple[str, ...]
    missing_cli_versions: int
    invalid_metadata_files: int


@dataclass(frozen=True, slots=True)
class _RunState:
    status: str
    timestamp: str
    detail_code: str | None


@dataclass(frozen=True, slots=True)
class _ReadModelState:
    key_id: str | None
    input_event_count: int
    effective_usage_count: int


@dataclass(frozen=True, slots=True)
class _LocalDiagnostics:
    database_version: int
    pending_outbox: int
    parser_issue_count: int
    parser_issue_codes: tuple[tuple[str, int], ...]
    unresolved_events: int
    unresolved_threads: int
    unresolved_reasons: tuple[tuple[str, int], ...]
    latest_collect: _RunState | None
    latest_sync: _RunState | None
    last_collect_success: str | None
    last_sync_success: str | None
    has_collect_history: bool
    has_sync_history: bool
    read_model: _ReadModelState | None


def run_doctor(config: AppConfig, shared_key: bytes) -> DoctorResult:
    checks: list[DoctorCheck] = []
    key_matches = key_id(shared_key) == config.key_id
    checks.append(
        DoctorCheck(
            "shared-key",
            CheckStatus.OK if key_matches else CheckStatus.ERROR,
            "configured key matches" if key_matches else "key mismatch",
        )
    )

    rollout_inventory, thread_inventory = _check_codex_sources(
        Path(config.codex_home),
        checks,
    )
    if rollout_inventory is not None:
        checks.append(_check_cli_versions(rollout_inventory))
    if rollout_inventory is not None and thread_inventory is not None:
        checks.extend(_check_source_join(rollout_inventory, thread_inventory))

    local_diagnostics = _check_local_state(Path(config.state_db), checks)
    if local_diagnostics is not None:
        checks.extend(_local_operational_checks(local_diagnostics))

    replay = _check_ledger(Path(config.ledger_root), config, checks)
    if replay is not None and local_diagnostics is not None:
        checks.append(_check_read_model(replay, local_diagnostics.read_model))

    return DoctorResult(tuple(checks))


def _check_codex_sources(
    codex_home: Path,
    checks: list[DoctorCheck],
) -> tuple[_RolloutInventory | None, ThreadInventory | None]:
    if not codex_home.is_dir():
        checks.append(
            DoctorCheck("codex-rollouts", CheckStatus.ERROR, "Codex home is missing")
        )
        checks.append(
            DoctorCheck("codex-state", CheckStatus.WARNING, "Codex SQLite state missing")
        )
        return None, None

    try:
        paths = discover_rollout_files(codex_home)
    except OSError:
        checks.append(
            DoctorCheck(
                "codex-rollouts",
                CheckStatus.ERROR,
                "Codex rollout discovery failed",
            )
        )
        return None, None
    checks.append(
        DoctorCheck(
            "codex-rollouts",
            CheckStatus.OK if paths else CheckStatus.WARNING,
            f"{len(paths)} rollout files discovered",
        )
    )
    rollout_inventory = _scan_rollout_inventory(paths)

    state_database = find_codex_state_database(codex_home)
    if state_database is None:
        checks.append(
            DoctorCheck(
                "codex-state",
                CheckStatus.WARNING,
                "Codex SQLite state missing; JSONL-only fallback will be used",
            )
        )
        return rollout_inventory, None
    try:
        inventory = load_thread_inventory(state_database)
    except SqliteAdapterError:
        checks.append(
            DoctorCheck(
                "codex-state",
                CheckStatus.ERROR,
                "Codex SQLite state cannot be read",
            )
        )
        return rollout_inventory, None
    checks.append(
        DoctorCheck(
            "codex-state",
            CheckStatus.OK,
            f"{len(inventory.threads)} threads and {len(inventory.spawn_edges)} spawn edges",
        )
    )
    if inventory.issues:
        checks.append(
            DoctorCheck(
                "codex-lineage",
                CheckStatus.WARNING,
                f"{len(inventory.issues)} SQLite lineage compatibility warnings",
            )
        )
    else:
        checks.append(
            DoctorCheck(
                "codex-lineage",
                CheckStatus.OK,
                "SQLite lineage tables are readable",
            )
        )
    return rollout_inventory, inventory


def _scan_rollout_inventory(paths: Iterable[Path]) -> _RolloutInventory:
    thread_ids: set[str] = set()
    versions: set[str] = set()
    missing_versions = 0
    invalid_files = 0
    for path in paths:
        try:
            metadata = _read_first_metadata(path)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, RolloutParseError):
            invalid_files += 1
            continue
        thread_ids.add(metadata.thread_id)
        if metadata.cli_version is None:
            missing_versions += 1
        else:
            versions.add(metadata.cli_version)
    return _RolloutInventory(
        thread_ids=frozenset(thread_ids),
        cli_versions=tuple(sorted(versions)),
        missing_cli_versions=missing_versions,
        invalid_metadata_files=invalid_files,
    )


def _read_first_metadata(path: Path):
    with path.open("r", encoding="utf-8") as source:
        for _ in range(256):
            line = source.readline(1_048_577)
            if not line:
                break
            if len(line) > 1_048_576:
                raise ValueError("rollout metadata line is too large")
            record = json.loads(line)
            if isinstance(record, dict) and record.get("type") == "session_meta":
                return parse_rollout((line,)).metadata
    raise RolloutParseError(1, "rollout does not contain early session_meta")


def _check_cli_versions(inventory: _RolloutInventory) -> DoctorCheck:
    problems = inventory.missing_cli_versions + inventory.invalid_metadata_files
    if not inventory.cli_versions:
        return DoctorCheck(
            "codex-versions",
            CheckStatus.WARNING,
            "no parseable CLI version metadata was found",
        )
    versions = ", ".join(inventory.cli_versions[:5])
    if len(inventory.cli_versions) > 5:
        versions += f" and {len(inventory.cli_versions) - 5} more"
    if problems:
        return DoctorCheck(
            "codex-versions",
            CheckStatus.WARNING,
            f"observed {versions}; {problems} rollout files lack usable metadata",
        )
    return DoctorCheck(
        "codex-versions",
        CheckStatus.OK,
        f"observed structurally parseable versions: {versions}",
    )


def _check_source_join(
    rollouts: _RolloutInventory,
    sqlite_inventory: ThreadInventory,
) -> tuple[DoctorCheck, ...]:
    sqlite_ids = set(sqlite_inventory.threads)
    sqlite_with_rollout = {
        thread_id
        for thread_id, record in sqlite_inventory.threads.items()
        if record.rollout_path is not None
    }
    missing_sqlite = len(set(rollouts.thread_ids) - sqlite_ids)
    missing_jsonl = len(sqlite_with_rollout - set(rollouts.thread_ids))
    if missing_sqlite or missing_jsonl:
        return (
            DoctorCheck(
                "codex-join",
                CheckStatus.WARNING,
                f"JSONL-only threads {missing_sqlite}; SQLite-only rollout threads {missing_jsonl}",
            ),
        )
    return (
        DoctorCheck(
            "codex-join",
            CheckStatus.OK,
            f"{len(rollouts.thread_ids)} rollout thread IDs join cleanly",
        ),
    )


def _check_local_state(
    path: Path,
    checks: list[DoctorCheck],
) -> _LocalDiagnostics | None:
    if not path.is_file():
        checks.append(
            DoctorCheck(
                "local-state",
                CheckStatus.WARNING,
                "local state database has not been created",
            )
        )
        return None
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            checks.append(
                DoctorCheck("local-state", CheckStatus.ERROR, "SQLite is invalid")
            )
            return None
        diagnostics = _load_local_diagnostics(connection)
    except sqlite3.Error:
        checks.append(
            DoctorCheck(
                "local-state",
                CheckStatus.ERROR,
                "local state database cannot be read",
            )
        )
        return None
    finally:
        if connection is not None:
            connection.close()

    if diagnostics.database_version > DATABASE_VERSION:
        status = CheckStatus.ERROR
        message = (
            f"SQLite schema {diagnostics.database_version} is newer than "
            f"supported {DATABASE_VERSION}"
        )
    elif diagnostics.database_version < DATABASE_VERSION:
        status = CheckStatus.WARNING
        message = (
            f"SQLite schema {diagnostics.database_version} needs local "
            f"upgrade to {DATABASE_VERSION}"
        )
    else:
        status = CheckStatus.OK
        message = f"SQLite integrity passed; schema {DATABASE_VERSION}"
    checks.append(DoctorCheck("local-state", status, message))
    return diagnostics


def _load_local_diagnostics(connection: sqlite3.Connection) -> _LocalDiagnostics:
    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    pending = _scalar(
        connection,
        "SELECT COUNT(*) FROM outbox_events WHERE flushed_at IS NULL",
        tables,
        "outbox_events",
    )
    parser_count = _scalar(
        connection,
        "SELECT COUNT(*) FROM parser_issues",
        tables,
        "parser_issues",
    )
    parser_codes = (
        tuple(
            (row["code"], int(row["count"]))
            for row in connection.execute(
                "SELECT code, COUNT(*) AS count FROM parser_issues "
                "GROUP BY code ORDER BY code"
            )
        )
        if "parser_issues" in tables
        else ()
    )
    unresolved_rows = (
        connection.execute(
            """
            SELECT project_resolution, COUNT(*) AS events,
                   COUNT(DISTINCT thread_key) AS threads
            FROM usage_events
            WHERE effective_project_id IS NULL
            GROUP BY project_resolution
            ORDER BY project_resolution
            """
        ).fetchall()
        if "usage_events" in tables
        else ()
    )
    unresolved_threads = _scalar(
        connection,
        "SELECT COUNT(DISTINCT thread_key) FROM usage_events "
        "WHERE effective_project_id IS NULL",
        tables,
        "usage_events",
    )
    read_model = None
    if "read_model_state" in tables:
        row = connection.execute(
            """
            SELECT key_id, input_event_count, effective_usage_count
            FROM read_model_state WHERE singleton = 1
            """
        ).fetchone()
        if row is not None:
            read_model = _ReadModelState(
                key_id=row["key_id"],
                input_event_count=int(row["input_event_count"]),
                effective_usage_count=int(row["effective_usage_count"]),
            )
    return _LocalDiagnostics(
        database_version=int(connection.execute("PRAGMA user_version").fetchone()[0]),
        pending_outbox=pending,
        parser_issue_count=parser_count,
        parser_issue_codes=parser_codes,
        unresolved_events=sum(int(row["events"]) for row in unresolved_rows),
        unresolved_threads=unresolved_threads,
        unresolved_reasons=tuple(
            (row["project_resolution"], int(row["events"]))
            for row in unresolved_rows
        ),
        latest_collect=_latest_run(connection, "collect_runs", tables),
        latest_sync=_latest_run(connection, "sync_runs", tables),
        last_collect_success=_last_success(connection, "collect_runs", tables),
        last_sync_success=_last_success(connection, "sync_runs", tables),
        has_collect_history="collect_runs" in tables,
        has_sync_history="sync_runs" in tables,
        read_model=read_model,
    )


def _scalar(
    connection: sqlite3.Connection,
    query: str,
    tables: set[str],
    required_table: str,
) -> int:
    if required_table not in tables:
        return 0
    return int(connection.execute(query).fetchone()[0])


def _latest_run(
    connection: sqlite3.Connection,
    table: str,
    tables: set[str],
) -> _RunState | None:
    if table not in tables:
        return None
    row = connection.execute(
        f"""
        SELECT status, COALESCE(finished_at, started_at) AS timestamp, detail_code
        FROM {table}
        ORDER BY run_id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return _RunState(row["status"], row["timestamp"], row["detail_code"])


def _last_success(
    connection: sqlite3.Connection,
    table: str,
    tables: set[str],
) -> str | None:
    if table not in tables:
        return None
    row = connection.execute(
        f"SELECT MAX(finished_at) FROM {table} WHERE status = 'succeeded'"
    ).fetchone()
    return row[0] if row is not None and isinstance(row[0], str) else None


def _local_operational_checks(
    diagnostics: _LocalDiagnostics,
) -> tuple[DoctorCheck, ...]:
    checks = [
        _run_history_check(
            "collection-history",
            diagnostics.latest_collect,
            diagnostics.last_collect_success,
            diagnostics.has_collect_history,
        ),
        _run_history_check(
            "sync-history",
            diagnostics.latest_sync,
            diagnostics.last_sync_success,
            diagnostics.has_sync_history,
        ),
        DoctorCheck(
            "outbox",
            CheckStatus.WARNING if diagnostics.pending_outbox else CheckStatus.OK,
            (
                f"{diagnostics.pending_outbox} events are waiting for ledger flush"
                if diagnostics.pending_outbox
                else "no pending outbox events"
            ),
        ),
    ]
    parser_summary = ", ".join(
        f"{code}={count}" for code, count in diagnostics.parser_issue_codes
    )
    checks.append(
        DoctorCheck(
            "parser-issues",
            CheckStatus.WARNING if diagnostics.parser_issue_count else CheckStatus.OK,
            (
                f"{diagnostics.parser_issue_count} parser warnings ({parser_summary})"
                if diagnostics.parser_issue_count
                else "no parser warnings recorded"
            ),
        )
    )
    reason_summary = ", ".join(
        f"{reason}={count}" for reason, count in diagnostics.unresolved_reasons
    )
    checks.append(
        DoctorCheck(
            "classification",
            CheckStatus.WARNING if diagnostics.unresolved_events else CheckStatus.OK,
            (
                f"{diagnostics.unresolved_events} unresolved events across "
                f"{diagnostics.unresolved_threads} thread groups ({reason_summary})"
                if diagnostics.unresolved_events
                else "all effective usage events have a project"
            ),
        )
    )
    return tuple(checks)


def _run_history_check(
    name: str,
    run: _RunState | None,
    last_success: str | None,
    table_available: bool,
) -> DoctorCheck:
    if not table_available:
        return DoctorCheck(name, CheckStatus.WARNING, "history table needs local upgrade")
    if run is None:
        return DoctorCheck(name, CheckStatus.WARNING, "no run has been recorded")
    if run.status == "succeeded":
        return DoctorCheck(name, CheckStatus.OK, f"last success at {run.timestamp}")
    detail = f" ({run.detail_code})" if run.detail_code else ""
    previous = f"; last success at {last_success}" if last_success else ""
    return DoctorCheck(
        name,
        CheckStatus.WARNING,
        f"latest run is {run.status}{detail} at {run.timestamp}{previous}",
    )


def _check_ledger(
    ledger_root: Path,
    config: AppConfig,
    checks: list[DoctorCheck],
) -> ReplayResult | None:
    if not ledger_root.is_dir():
        checks.append(
            DoctorCheck("ledger", CheckStatus.ERROR, "ledger root is missing")
        )
        return None
    replay: ReplayResult | None = None
    try:
        read = LedgerReader(ledger_root).read_all()
        replay = replay_ledger_events(read.events, expected_key_id=config.key_id)
        checks.append(
            DoctorCheck(
                "ledger",
                CheckStatus.WARNING if read.issues else CheckStatus.OK,
                f"{replay.input_event_count} validated events; "
                f"{len(read.issues)} partial lines",
            )
        )
    except (
        LedgerIoError,
        LedgerReplayError,
        LedgerSchemaError,
        PrivacyViolation,
    ):
        checks.append(
            DoctorCheck("ledger", CheckStatus.ERROR, "ledger validation failed")
        )
    checks.extend(_check_git_repository(ledger_root, config.device_id))
    return replay


def _check_read_model(
    replay: ReplayResult,
    state: _ReadModelState | None,
) -> DoctorCheck:
    if state is None:
        return DoctorCheck(
            "read-model",
            CheckStatus.WARNING,
            "local read model has not been built",
        )
    expected_usage = len(replay.usage_events)
    matches = (
        state.key_id == replay.key_id
        and state.input_event_count == replay.input_event_count
        and state.effective_usage_count == expected_usage
    )
    return DoctorCheck(
        "read-model",
        CheckStatus.OK if matches else CheckStatus.WARNING,
        (
            "local read model matches the ledger"
            if matches
            else "local read model is stale; run collect or sync"
        ),
    )


def _check_git_repository(
    path: Path,
    device_id: str,
) -> tuple[DoctorCheck, ...]:
    repository = GitLedgerRepository(path, timeout_seconds=15)
    try:
        branch = repository.validate()
    except GitSyncError as error:
        if error.code == "git_not_found":
            return (
                DoctorCheck("ledger-git", CheckStatus.ERROR, "Git is not installed"),
            )
        if error.code in {"git_rev-parse_failed", "ledger_not_git_repository"}:
            return (
                DoctorCheck(
                    "ledger-git",
                    CheckStatus.WARNING,
                    "ledger is not a Git repository yet",
                ),
            )
        return (
            DoctorCheck(
                "ledger-git",
                CheckStatus.ERROR,
                f"Git sync is not ready ({error.code})",
            ),
        )

    try:
        changes = repository.working_changes()
        repository.validate_own_changes(changes, device_id)
        git_check = DoctorCheck(
            "ledger-git",
            CheckStatus.WARNING if changes else CheckStatus.OK,
            (
                f"Git is ready on {branch}; {len(changes)} device files await sync"
                if changes
                else f"Git is clean and ready on {branch}"
            ),
        )
    except GitSyncError as error:
        git_check = DoctorCheck(
            "ledger-git",
            CheckStatus.ERROR,
            f"unsafe Git worktree state ({error.code})",
        )

    try:
        repository.check_remote_access()
        remote_check = DoctorCheck(
            "ledger-remote",
            CheckStatus.OK,
            "origin is reachable with current credentials",
        )
    except GitSyncError as error:
        remote_check = DoctorCheck(
            "ledger-remote",
            CheckStatus.ERROR,
            f"origin read access failed ({error.code})",
        )
    return git_check, remote_check
