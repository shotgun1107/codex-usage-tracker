from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
from types import MappingProxyType
import unittest

from codex_usage.storage.sqlite import (
    CursorRegression,
    LocalStateStore,
    LocalStoreError,
    OutboxConflict,
    ParserIssueRecord,
    SourceCursor,
    UnsupportedDatabaseVersion,
)


def cursor(
    *,
    offset: int = 10,
    fingerprint: str = "fingerprint-one",
    path: str = "C:/local/rollout.jsonl",
) -> SourceCursor:
    return SourceCursor(
        source_id="source-one",
        source_path=path,
        fingerprint=fingerprint,
        byte_offset=offset,
        last_complete_line_digest="digest",
    )


def event(event_id: str, *, value: int = 1) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "usage_checkpoint",
        "event_id": event_id,
        "value": value,
        "label": "한글",
    }


class LocalStateStoreTests(unittest.TestCase):
    def test_collection_atomically_stores_cursor_outbox_and_parser_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")

            inserted = store.store_collection(
                cursor(),
                (event("event-two"), event("event-one")),
                parser_issues=(
                    ParserIssueRecord("source-one", "weak_key", 4, "0.1"),
                ),
            )

            self.assertEqual(inserted, 2)
            self.assertEqual(store.get_cursor("source-one"), cursor())
            self.assertEqual(dict(store.all_cursors()), {"source-one": cursor()})
            self.assertEqual(
                [item.event_id for item in store.pending_outbox()],
                ["event-two", "event-one"],
            )
            self.assertEqual(store.pending_outbox()[0].payload()["label"], "한글")
            self.assertEqual(store.parser_issue_count(), 1)
            self.assertEqual(store.outbox_counts().pending, 2)
            self.assertEqual(store.known_usage_source_event_ids(), frozenset())

            store.store_collection(
                cursor(offset=20),
                (),
                parser_issues=(ParserIssueRecord("source-one", "nullable"),),
            )
            store.store_collection(
                cursor(offset=30),
                (),
                parser_issues=(ParserIssueRecord("source-one", "nullable"),),
            )
            self.assertEqual(store.parser_issue_count(), 2)

    def test_recollection_is_idempotent_and_can_advance_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.store_collection(
                cursor(),
                (MappingProxyType(event("event-one")),),
            )

            inserted = store.store_collection(
                cursor(offset=20),
                (event("event-one"),),
            )

            self.assertEqual(inserted, 0)
            stored_cursor = store.get_cursor("source-one")
            self.assertIsNotNone(stored_cursor)
            assert stored_cursor is not None
            self.assertEqual(stored_cursor.byte_offset, 20)
            self.assertEqual(store.outbox_counts().pending, 1)

    def test_known_usage_source_ids_include_pending_and_flushed_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            usage = event("event-one")
            usage["source_event_id"] = "source-event-one"
            store.store_collection(cursor(), (usage,))

            self.assertEqual(
                store.known_usage_source_event_ids(),
                frozenset({"source-event-one"}),
            )
            store.mark_outbox_flushed(
                {"event-one": "devices/device/usage/2026/08/26.jsonl"}
            )
            self.assertEqual(
                store.known_usage_source_event_ids(),
                frozenset({"source-event-one"}),
            )

    def test_event_conflict_rolls_back_cursor_and_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.store_collection(cursor(), (event("event-one"),))

            with self.assertRaises(OutboxConflict):
                store.store_collection(
                    cursor(offset=30),
                    (event("new-event"), event("event-one", value=2)),
                )

            stored_cursor = store.get_cursor("source-one")
            self.assertIsNotNone(stored_cursor)
            assert stored_cursor is not None
            self.assertEqual(stored_cursor.byte_offset, 10)
            self.assertEqual(
                [item.event_id for item in store.pending_outbox()],
                ["event-one"],
            )

    def test_cursor_cannot_regress_unless_source_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.store_collection(cursor(offset=100), ())

            with self.assertRaises(CursorRegression):
                store.store_collection(cursor(offset=99), ())

            store.store_collection(
                cursor(offset=5, fingerprint="replacement-fingerprint"),
                (),
            )
            stored_cursor = store.get_cursor("source-one")
            self.assertIsNotNone(stored_cursor)
            assert stored_cursor is not None
            self.assertEqual(stored_cursor.byte_offset, 5)

    def test_flushing_is_atomic_idempotent_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.store_collection(
                cursor(),
                (event("event-one"), event("event-two")),
            )

            store.mark_outbox_flushed(
                {"event-one": "devices/device/usage/2026/08/26.jsonl"}
            )
            store.mark_outbox_flushed(
                {"event-one": "devices/device/usage/2026/08/26.jsonl"}
            )

            self.assertEqual(store.outbox_counts().pending, 1)
            self.assertEqual(store.outbox_counts().flushed, 1)
            self.assertEqual(store.pending_outbox()[0].event_id, "event-two")

            with self.assertRaises(OutboxConflict):
                store.mark_outbox_flushed(
                    {"event-one": "devices/device/usage/other.jsonl"}
                )
            with self.assertRaises(LocalStoreError):
                store.mark_outbox_flushed({"unknown": "devices/device/file.jsonl"})
            with self.assertRaises(ValueError):
                store.mark_outbox_flushed({"event-two": "../outside.jsonl"})
            with self.assertRaises(ValueError):
                store.mark_outbox_flushed({"event-two": "C:/outside.jsonl"})
            with self.assertRaises(ValueError):
                store.mark_outbox_flushed({"event-two": "devices/bad\nname.jsonl"})

    def test_state_survives_reopen_and_newer_database_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            LocalStateStore(path).store_collection(
                cursor(),
                (event("event-one"),),
            )

            reopened = LocalStateStore(path)
            self.assertEqual(reopened.outbox_counts().pending, 1)

            future = Path(directory) / "future.sqlite"
            connection = sqlite3.connect(future)
            connection.execute("PRAGMA user_version = 999")
            connection.close()
            with self.assertRaises(UnsupportedDatabaseVersion):
                LocalStateStore(future)

    def test_collect_and_sync_run_history_and_independent_parser_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            store = LocalStateStore(path)
            collect_run = store.begin_collect_run()
            sync_run = store.begin_sync_run()

            store.finish_collect_run(collect_run, "succeeded")
            store.finish_sync_run(sync_run, "failed", "git_fetch_failed")
            inserted = store.record_parser_issues(
                (
                    ParserIssueRecord(
                        "source-one",
                        "fatal_rollout_parse_error",
                        7,
                    ),
                )
            )
            repeated = store.record_parser_issues(
                (
                    ParserIssueRecord(
                        "source-one",
                        "fatal_rollout_parse_error",
                        7,
                    ),
                )
            )

            self.assertEqual((inserted, repeated), (1, 0))
            self.assertEqual(store.parser_issue_count(), 1)
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT status FROM collect_runs WHERE run_id = ?",
                        (collect_run,),
                    ).fetchone()[0],
                    "succeeded",
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT status, detail_code FROM sync_runs WHERE run_id = ?",
                        (sync_run,),
                    ).fetchone(),
                    ("failed", "git_fetch_failed"),
                )
            finally:
                connection.close()

    def test_schema_version_one_is_upgraded_with_collect_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 1")
            connection.close()

            store = LocalStateStore(path)
            run_id = store.begin_collect_run()
            store.finish_collect_run(run_id, "succeeded")

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM collect_runs").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_invalid_cursor_issue_limit_and_event_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            cursor(offset=-1)
        with self.assertRaises(ValueError):
            ParserIssueRecord("source", "code", -1)
        with self.assertRaises(ValueError):
            ParserIssueRecord("source", "code", cli_version="")

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            with self.assertRaises(ValueError):
                store.pending_outbox(limit=0)
            with self.assertRaises(ValueError):
                store.store_collection(cursor(), ({"event_id": "x"},))
            with self.assertRaises(ValueError):
                store.store_collection(
                    cursor(),
                    (),
                    parser_issues=(ParserIssueRecord("other", "code"),),
                )


if __name__ == "__main__":
    unittest.main()
