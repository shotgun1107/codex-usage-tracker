from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_usage.sources.codex_sqlite import (
    SqliteAdapterError,
    load_thread_inventory,
)


class CodexSqliteAdapterTests(unittest.TestCase):
    def test_current_shape_loads_threads_and_spawn_edges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT,
                    source TEXT,
                    cwd TEXT,
                    tokens_used INTEGER,
                    git_origin_url TEXT,
                    git_branch TEXT,
                    git_sha TEXT,
                    cli_version TEXT,
                    model TEXT,
                    reasoning_effort TEXT,
                    agent_path TEXT
                );
                CREATE TABLE thread_spawn_edges (
                    parent_thread_id TEXT,
                    child_thread_id TEXT,
                    status TEXT
                );
                INSERT INTO threads VALUES (
                    'parent', 'parent.jsonl', 'vscode', 'C:/repo', 100,
                    'https://github.com/example/repo.git', 'main', 'abc',
                    '0.150.0', 'model-a', 'low', '/root'
                );
                INSERT INTO threads VALUES (
                    'child', 'child.jsonl', 'subagent', 'C:/repo', 40,
                    'https://github.com/example/repo.git', 'main', 'def',
                    '0.150.0', 'model-b', 'medium', '/root/child'
                );
                INSERT INTO thread_spawn_edges VALUES (
                    'parent', 'child', 'completed'
                );
                """
            )
            connection.commit()
            connection.close()

            inventory = load_thread_inventory(path)

            self.assertEqual(set(inventory.threads), {"parent", "child"})
            self.assertEqual(inventory.threads["parent"].tokens_used, 100)
            self.assertEqual(inventory.threads["child"].model, "model-b")
            self.assertEqual(len(inventory.spawn_edges), 1)
            self.assertEqual(
                inventory.spawn_edges[0].parent_thread_id,
                "parent",
            )
            self.assertEqual(inventory.spawn_edges[0].status, "completed")
            self.assertEqual(inventory.issues, ())

    def test_minimal_legacy_shape_fills_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY);
                INSERT INTO threads VALUES ('legacy-thread');
                """
            )
            connection.commit()
            connection.close()

            inventory = load_thread_inventory(path)

            thread = inventory.threads["legacy-thread"]
            self.assertIsNone(thread.rollout_path)
            self.assertIsNone(thread.git_origin_url)
            self.assertEqual(
                inventory.issues[0].code,
                "spawn_edge_table_missing",
            )

    def test_spawn_edge_table_with_missing_columns_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY);
                CREATE TABLE thread_spawn_edges (parent_thread_id TEXT);
                INSERT INTO threads VALUES ('thread');
                """
            )
            connection.commit()
            connection.close()

            inventory = load_thread_inventory(path)

            self.assertEqual(inventory.spawn_edges, ())
            self.assertEqual(
                inventory.issues[0].code,
                "spawn_edge_columns_missing",
            )

    def test_thread_mapping_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY);
                INSERT INTO threads VALUES ('thread');
                """
            )
            connection.commit()
            connection.close()

            inventory = load_thread_inventory(path)

            with self.assertRaises(TypeError):
                inventory.threads["other"] = inventory.threads["thread"]  # type: ignore[index]

    def test_missing_database_threads_table_and_invalid_values_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sqlite"
            with self.assertRaises(SqliteAdapterError):
                load_thread_inventory(missing)

            empty = Path(directory) / "empty.sqlite"
            sqlite3.connect(empty).close()
            with self.assertRaises(SqliteAdapterError):
                load_thread_inventory(empty)

            invalid = Path(directory) / "invalid.sqlite"
            connection = sqlite3.connect(invalid)
            connection.executescript(
                """
                CREATE TABLE threads (id TEXT PRIMARY KEY, tokens_used INTEGER);
                INSERT INTO threads VALUES ('thread', -1);
                """
            )
            connection.commit()
            connection.close()
            with self.assertRaises(SqliteAdapterError):
                load_thread_inventory(invalid)

    def test_negative_busy_timeout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            connection.commit()
            connection.close()

            with self.assertRaises(SqliteAdapterError):
                load_thread_inventory(path, busy_timeout_ms=-1)


if __name__ == "__main__":
    unittest.main()
