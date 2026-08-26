"""Read-only adapter for Codex's local SQLite thread state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import sqlite3


class SqliteAdapterError(RuntimeError):
    """Raised when the Codex state database cannot be read safely."""


@dataclass(frozen=True, slots=True)
class ThreadRecord:
    """The stable subset of a row in Codex's threads table."""

    thread_id: str
    rollout_path: str | None
    source: str | None
    cwd: str | None
    tokens_used: int | None
    git_origin_url: str | None
    git_branch: str | None
    git_sha: str | None
    cli_version: str | None
    model: str | None
    reasoning_effort: str | None
    agent_path: str | None


@dataclass(frozen=True, slots=True)
class SpawnEdge:
    """A direct parent-child relationship from thread_spawn_edges."""

    parent_thread_id: str
    child_thread_id: str
    status: str | None


@dataclass(frozen=True, slots=True)
class InventoryIssue:
    """A non-fatal state-database compatibility issue."""

    code: str


@dataclass(frozen=True, slots=True)
class ThreadInventory:
    """A consistent read snapshot of Codex thread state."""

    threads: Mapping[str, ThreadRecord]
    spawn_edges: tuple[SpawnEdge, ...]
    issues: tuple[InventoryIssue, ...]


_THREAD_COLUMNS = (
    "id",
    "rollout_path",
    "source",
    "cwd",
    "tokens_used",
    "git_origin_url",
    "git_branch",
    "git_sha",
    "cli_version",
    "model",
    "reasoning_effort",
    "agent_path",
)


def load_thread_inventory(
    database_path: str | Path,
    *,
    busy_timeout_ms: int = 2_000,
) -> ThreadInventory:
    """Load threads and spawn edges from a read-only SQLite snapshot."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise SqliteAdapterError("Codex state database does not exist")
    if busy_timeout_ms < 0:
        raise SqliteAdapterError("busy_timeout_ms must not be negative")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        connection.execute("BEGIN")

        tables = _table_names(connection)
        if "threads" not in tables:
            raise SqliteAdapterError("Codex state database has no threads table")

        thread_columns = _column_names(connection, "threads")
        if "id" not in thread_columns:
            raise SqliteAdapterError("Codex threads table has no id column")

        issues: list[InventoryIssue] = []
        threads = _load_threads(connection, thread_columns)

        if "thread_spawn_edges" in tables:
            edge_columns = _column_names(connection, "thread_spawn_edges")
            required_edges = {"parent_thread_id", "child_thread_id"}
            if required_edges.issubset(edge_columns):
                spawn_edges = _load_spawn_edges(connection, edge_columns)
            else:
                spawn_edges = ()
                issues.append(InventoryIssue("spawn_edge_columns_missing"))
        else:
            spawn_edges = ()
            issues.append(InventoryIssue("spawn_edge_table_missing"))

        connection.rollback()
        return ThreadInventory(
            threads=MappingProxyType(threads),
            spawn_edges=spawn_edges,
            issues=tuple(issues),
        )
    except sqlite3.Error as error:
        raise SqliteAdapterError("failed to read Codex state database") from error
    finally:
        if connection is not None:
            connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row["name"]) for row in rows}


def _load_threads(
    connection: sqlite3.Connection,
    available_columns: set[str],
) -> dict[str, ThreadRecord]:
    expressions = [
        f'"{column}"' if column in available_columns else f"NULL AS \"{column}\""
        for column in _THREAD_COLUMNS
    ]
    rows = connection.execute(
        f"SELECT {', '.join(expressions)} FROM threads"
    ).fetchall()

    threads: dict[str, ThreadRecord] = {}
    for row in rows:
        thread_id = row["id"]
        if not isinstance(thread_id, str) or not thread_id:
            raise SqliteAdapterError("threads.id contains an invalid value")
        if thread_id in threads:
            raise SqliteAdapterError("threads.id contains a duplicate value")
        threads[thread_id] = ThreadRecord(
            thread_id=thread_id,
            rollout_path=_optional_string(row["rollout_path"]),
            source=_optional_string(row["source"]),
            cwd=_optional_string(row["cwd"]),
            tokens_used=_optional_non_negative_int(row["tokens_used"], "tokens_used"),
            git_origin_url=_optional_string(row["git_origin_url"]),
            git_branch=_optional_string(row["git_branch"]),
            git_sha=_optional_string(row["git_sha"]),
            cli_version=_optional_string(row["cli_version"]),
            model=_optional_string(row["model"]),
            reasoning_effort=_optional_string(row["reasoning_effort"]),
            agent_path=_optional_string(row["agent_path"]),
        )
    return threads


def _load_spawn_edges(
    connection: sqlite3.Connection,
    available_columns: set[str],
) -> tuple[SpawnEdge, ...]:
    status_expression = '"status"' if "status" in available_columns else "NULL"
    rows = connection.execute(
        "SELECT parent_thread_id, child_thread_id, "
        f"{status_expression} AS status FROM thread_spawn_edges"
    ).fetchall()

    edges: list[SpawnEdge] = []
    for row in rows:
        parent = row["parent_thread_id"]
        child = row["child_thread_id"]
        if not isinstance(parent, str) or not parent:
            raise SqliteAdapterError("spawn edge contains an invalid parent ID")
        if not isinstance(child, str) or not child:
            raise SqliteAdapterError("spawn edge contains an invalid child ID")
        edges.append(
            SpawnEdge(
                parent_thread_id=parent,
                child_thread_id=child,
                status=_optional_string(row["status"]),
            )
        )
    return tuple(edges)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_non_negative_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SqliteAdapterError(f"{field_name} contains an invalid value")
    return value
