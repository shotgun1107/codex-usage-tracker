"""Crash-safe local SQLite state for collection and ledger replay."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
from types import MappingProxyType

from codex_usage.ledger.replay import ReplayResult
from codex_usage.storage.read_model import (
    DailyUsageRow,
    ReadModelCounts,
    ReadModelDataError,
    ReadModelState,
    load_daily_usage_utc,
    load_read_model_counts,
    load_read_model_state,
    replace_read_model,
)
from codex_usage.storage.schema import DATABASE_VERSION, SCHEMA_SQL


class LocalStoreError(RuntimeError):
    """Base error for local state failures."""


class UnsupportedDatabaseVersion(LocalStoreError):
    """The database was created by a newer incompatible application."""


class OutboxConflict(LocalStoreError):
    """One event ID was reused with different sanitized content."""


class CursorRegression(LocalStoreError):
    """A cursor attempted to move backwards within the same source version."""


@dataclass(frozen=True, slots=True)
class SourceCursor:
    """The last complete position consumed from one local source file."""

    source_id: str
    source_path: str
    fingerprint: str
    byte_offset: int
    last_complete_line_digest: str | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.source_id, "source_id"),
            (self.source_path, "source_path"),
            (self.fingerprint, "fingerprint"),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must not be empty")
        if (
            isinstance(self.byte_offset, bool)
            or not isinstance(self.byte_offset, int)
            or self.byte_offset < 0
        ):
            raise ValueError("byte_offset must be a non-negative integer")
        if self.last_complete_line_digest is not None and (
            not isinstance(self.last_complete_line_digest, str)
            or not self.last_complete_line_digest
        ):
            raise ValueError(
                "last_complete_line_digest must be a non-empty string or None"
            )


@dataclass(frozen=True, slots=True)
class ParserIssueRecord:
    """A privacy-safe parser diagnostic stored with a collection checkpoint."""

    source_id: str
    code: str
    record_position: int | None = None
    cli_version: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.code:
            raise ValueError("parser issue source_id and code must not be empty")
        if self.record_position is not None and (
            isinstance(self.record_position, bool)
            or not isinstance(self.record_position, int)
            or self.record_position < 0
        ):
            raise ValueError("record_position must be non-negative or None")
        if self.cli_version is not None and (
            not isinstance(self.cli_version, str) or not self.cli_version
        ):
            raise ValueError("cli_version must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class PendingOutboxEvent:
    """One sanitized event waiting to be appended to the Git ledger."""

    sequence: int
    event_id: str
    event_type: str
    payload_json: str

    def payload(self) -> dict[str, object]:
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise LocalStoreError("outbox payload is not an object")
        return value


@dataclass(frozen=True, slots=True)
class OutboxCounts:
    pending: int
    flushed: int


class LocalStateStore:
    """Owns operational state and the disposable SQLite read model."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self.path = Path(database_path).expanduser().resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get_cursor(self, source_id: str) -> SourceCursor | None:
        if not source_id:
            raise ValueError("source_id must not be empty")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT source_id, source_path, fingerprint, byte_offset,
                       last_complete_line_digest
                FROM source_cursors
                WHERE source_id = ?
                """,
                (source_id,),
            ).fetchone()
        if row is None:
            return None
        return SourceCursor(
            source_id=row["source_id"],
            source_path=row["source_path"],
            fingerprint=row["fingerprint"],
            byte_offset=row["byte_offset"],
            last_complete_line_digest=row["last_complete_line_digest"],
        )

    def all_cursors(self) -> Mapping[str, SourceCursor]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT source_id, source_path, fingerprint, byte_offset,
                       last_complete_line_digest
                FROM source_cursors
                """
            ).fetchall()
        return MappingProxyType(
            {
                row["source_id"]: SourceCursor(
                    source_id=row["source_id"],
                    source_path=row["source_path"],
                    fingerprint=row["fingerprint"],
                    byte_offset=row["byte_offset"],
                    last_complete_line_digest=row["last_complete_line_digest"],
                )
                for row in rows
            }
        )

    def store_collection(
        self,
        cursor: SourceCursor,
        events: Iterable[Mapping[str, object]],
        *,
        parser_issues: Iterable[ParserIssueRecord] = (),
    ) -> int:
        """Atomically enqueue sanitized events and advance their source cursor."""

        encoded_events = tuple(_encode_outbox_event(event) for event in events)
        issues = tuple(parser_issues)
        if any(issue.source_id != cursor.source_id for issue in issues):
            raise ValueError("parser issue source_id must match the cursor")

        connection = self._connect()
        inserted = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT fingerprint, byte_offset FROM source_cursors WHERE source_id = ?",
                (cursor.source_id,),
            ).fetchone()
            if (
                previous is not None
                and previous["fingerprint"] == cursor.fingerprint
                and cursor.byte_offset < previous["byte_offset"]
            ):
                raise CursorRegression(
                    "cursor cannot move backwards for an unchanged source"
                )

            now = _utc_now()
            inserted = _enqueue_encoded_events(connection, encoded_events, now)

            for issue in issues:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO parser_issues (
                        source_id, code, record_position, cli_version, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        issue.source_id,
                        issue.code,
                        issue.record_position if issue.record_position is not None else -1,
                        issue.cli_version or "",
                        now,
                    ),
                )

            connection.execute(
                """
                INSERT INTO source_cursors (
                    source_id, source_path, fingerprint, byte_offset,
                    last_complete_line_digest, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    source_path = excluded.source_path,
                    fingerprint = excluded.fingerprint,
                    byte_offset = excluded.byte_offset,
                    last_complete_line_digest = excluded.last_complete_line_digest,
                    updated_at = excluded.updated_at
                """,
                (
                    cursor.source_id,
                    cursor.source_path,
                    cursor.fingerprint,
                    cursor.byte_offset,
                    cursor.last_complete_line_digest,
                    now,
                ),
            )
            connection.commit()
            return inserted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue_outbox_events(
        self,
        events: Iterable[Mapping[str, object]],
    ) -> int:
        """Atomically enqueue sanitized non-source events without a cursor."""

        encoded_events = tuple(_encode_outbox_event(event) for event in events)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = _enqueue_encoded_events(
                connection,
                encoded_events,
                _utc_now(),
            )
            connection.commit()
            return inserted
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def pending_outbox(self, *, limit: int = 1_000) -> tuple[PendingOutboxEvent, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_id, event_type, payload_json
                FROM outbox_events
                WHERE flushed_at IS NULL
                ORDER BY sequence
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            PendingOutboxEvent(
                sequence=row["sequence"],
                event_id=row["event_id"],
                event_type=row["event_type"],
                payload_json=row["payload_json"],
            )
            for row in rows
        )

    def mark_outbox_flushed(self, ledger_paths: Mapping[str, str]) -> None:
        """Mark events durable after their complete ledger lines are fsynced."""

        normalized = {
            event_id: _validate_ledger_path(path)
            for event_id, path in ledger_paths.items()
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            for event_id, ledger_path in normalized.items():
                row = connection.execute(
                    "SELECT ledger_path, flushed_at FROM outbox_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise LocalStoreError("cannot flush an unknown outbox event")
                if row["flushed_at"] is not None:
                    if row["ledger_path"] != ledger_path:
                        raise OutboxConflict(
                            "event was already flushed to a different ledger path"
                        )
                    continue
                connection.execute(
                    """
                    UPDATE outbox_events
                    SET ledger_path = ?, flushed_at = ?
                    WHERE event_id = ?
                    """,
                    (ledger_path, now, event_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def outbox_counts(self) -> OutboxCounts:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN flushed_at IS NULL THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN flushed_at IS NOT NULL THEN 1 ELSE 0 END) AS flushed
                FROM outbox_events
                """
            ).fetchone()
        return OutboxCounts(pending=row["pending"] or 0, flushed=row["flushed"] or 0)

    def parser_issue_count(self) -> int:
        with closing(self._connect()) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM parser_issues").fetchone()[0]
            )

    def begin_sync_run(self) -> int:
        """Record a local sync attempt without placing secrets in diagnostics."""

        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO sync_runs (started_at, status)
                VALUES (?, 'running')
                """,
                (_utc_now(),),
            )
            return int(cursor.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        status: str,
        detail_code: str | None = None,
    ) -> None:
        """Finish one known sync run as succeeded or failed."""

        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            raise ValueError("run_id must be a positive integer")
        if status not in {"succeeded", "failed"}:
            raise ValueError("sync status must be succeeded or failed")
        if detail_code is not None and (
            not isinstance(detail_code, str) or not detail_code
        ):
            raise ValueError("detail_code must be a non-empty string or None")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE sync_runs
                SET finished_at = ?, status = ?, detail_code = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (_utc_now(), status, detail_code, run_id),
            )
            if cursor.rowcount != 1:
                raise LocalStoreError("sync run is missing or already finished")

    def known_usage_source_event_ids(self) -> frozenset[str]:
        """Return logical usage IDs already retained in the local outbox history."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM outbox_events
                WHERE event_type = 'usage_checkpoint'
                """
            ).fetchall()
        source_ids: set[str] = set()
        for row in rows:
            try:
                event = json.loads(row["payload_json"])
            except json.JSONDecodeError as error:
                raise LocalStoreError("outbox contains invalid JSON") from error
            source_id = event.get("source_event_id") if isinstance(event, dict) else None
            if isinstance(source_id, str) and source_id:
                source_ids.add(source_id)
        return frozenset(source_ids)

    def rebuild_read_model(self, replay: ReplayResult) -> ReadModelState:
        """Atomically replace disposable query tables from a replay result."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT generation FROM read_model_state WHERE singleton = 1"
            ).fetchone()
            generation = (int(previous["generation"]) if previous else 0) + 1
            state = replace_read_model(
                connection,
                replay,
                generation=generation,
            )
            connection.commit()
            return state
        except ReadModelDataError as error:
            connection.rollback()
            raise LocalStoreError(str(error)) from error
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_model_state(self) -> ReadModelState | None:
        with closing(self._connect()) as connection:
            return load_read_model_state(connection)

    def read_model_counts(self) -> ReadModelCounts:
        with closing(self._connect()) as connection:
            return load_read_model_counts(connection)

    def daily_usage_utc(self) -> tuple[DailyUsageRow, ...]:
        with closing(self._connect()) as connection:
            return load_daily_usage_utc(connection)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_VERSION:
                raise UnsupportedDatabaseVersion(
                    f"database version {version} is newer than supported version "
                    f"{DATABASE_VERSION}"
                )
            connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + SCHEMA_SQL
                + f"\nPRAGMA user_version = {DATABASE_VERSION};\nCOMMIT;"
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


def _encode_outbox_event(event: Mapping[str, object]) -> tuple[str, str, str]:
    if not isinstance(event, Mapping):
        raise ValueError("outbox event must be a mapping")
    event_id = event.get("event_id")
    event_type = event.get("event_type")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("outbox event_id must not be empty")
    if event_type not in {"usage_checkpoint", "mapping", "quota_snapshot"}:
        raise ValueError("outbox event_type is unsupported")
    try:
        payload_json = json.dumps(
            dict(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("outbox event is not canonical JSON") from error
    return event_id, event_type, payload_json


def _enqueue_encoded_events(
    connection: sqlite3.Connection,
    encoded_events: Iterable[tuple[str, str, str]],
    enqueued_at: str,
) -> int:
    inserted = 0
    for event_id, event_type, payload_json in encoded_events:
        existing = connection.execute(
            "SELECT payload_json FROM outbox_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO outbox_events (
                    event_id, event_type, payload_json, enqueued_at
                ) VALUES (?, ?, ?, ?)
                """,
                (event_id, event_type, payload_json, enqueued_at),
            )
            inserted += 1
        elif existing["payload_json"] != payload_json:
            raise OutboxConflict("event_id already exists with different content")
    return inserted


def _validate_ledger_path(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("ledger path must not be empty")
    if any(character in path for character in ("\x00", "\r", "\n")):
        raise ValueError("ledger path contains a control character")
    normalized = path.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(normalized)
    if (
        pure_path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in pure_path.parts
        or "." in pure_path.parts
    ):
        raise ValueError("ledger path must be a safe relative path")
    return pure_path.as_posix()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
