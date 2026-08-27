from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_usage.ledger.jsonl import (
    LedgerEventConflict,
    LedgerIoError,
    LedgerReader,
    LedgerWriter,
    ledger_relative_path,
)
from codex_usage.ledger.replay import replay_ledger_events
from codex_usage.privacy.guard import PrivacyViolation
from codex_usage.storage.sqlite import LocalStateStore, SourceCursor
from tests.ledger_events import (
    DEVICE_ID,
    KEY_ID,
    PROJECT_MANUAL,
    mapping_event,
    opaque,
    quota_event,
    usage_event,
)


def source_cursor(offset: int = 100) -> SourceCursor:
    return SourceCursor(
        source_id="source",
        source_path="C:/local/rollout.jsonl",
        fingerprint="fingerprint",
        byte_offset=offset,
        last_complete_line_digest="digest",
    )


def canonical(event: dict[str, object]) -> bytes:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class FailFirstMarkStore:
    def __init__(self, store: LocalStateStore) -> None:
        self.store = store
        self.failed = False

    def pending_outbox(self, *, limit: int = 1_000):
        return self.store.pending_outbox(limit=limit)

    def mark_outbox_flushed(self, ledger_paths):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after fsync")
        self.store.mark_outbox_flushed(ledger_paths)


class LedgerJsonlIntegrationTests(unittest.TestCase):
    def test_outbox_to_jsonl_to_replay_to_read_model(self) -> None:
        events = (usage_event(), mapping_event(), quota_event())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = root / "ledger"
            store = LocalStateStore(root / "state.sqlite")
            store.store_collection(source_cursor(), events)

            flush = LedgerWriter(ledger_root, DEVICE_ID).flush(store)

            self.assertEqual(flush.pending_seen, 3)
            self.assertEqual(flush.appended, 3)
            self.assertEqual(store.outbox_counts().pending, 0)
            self.assertTrue(
                (
                    ledger_root
                    / "devices"
                    / DEVICE_ID
                    / "usage"
                    / "2026"
                    / "08"
                    / "26.jsonl"
                ).is_file()
            )
            self.assertTrue(
                (
                    ledger_root
                    / "devices"
                    / DEVICE_ID
                    / "mappings"
                    / "2026"
                    / "08.jsonl"
                ).is_file()
            )

            read = LedgerReader(ledger_root).read_all()
            self.assertEqual(len(read.events), 3)
            self.assertEqual(read.issues, ())
            replay = replay_ledger_events(read.events, expected_key_id=KEY_ID)
            store.rebuild_read_model(replay)

            self.assertEqual(store.read_model_counts().usage, 1)
            self.assertEqual(store.read_model_counts().mappings, 1)
            self.assertEqual(store.read_model_counts().quota, 1)
            self.assertEqual(
                store.daily_usage_utc()[0].effective_project_id,
                PROJECT_MANUAL,
            )

    def test_crash_after_file_fsync_does_not_duplicate_event(self) -> None:
        event = usage_event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = root / "ledger"
            store = LocalStateStore(root / "state.sqlite")
            store.store_collection(source_cursor(), (event,))
            writer = LedgerWriter(ledger_root, DEVICE_ID)

            with self.assertRaises(RuntimeError):
                writer.flush(FailFirstMarkStore(store))

            self.assertEqual(store.outbox_counts().pending, 1)
            retry = writer.flush(store)
            self.assertEqual(retry.appended, 0)
            self.assertEqual(retry.already_present, 1)
            self.assertEqual(store.outbox_counts().pending, 0)
            self.assertEqual(len(LedgerReader(ledger_root).read_all().events), 1)

    def test_reader_ignores_and_writer_recovers_partial_final_line(self) -> None:
        existing = usage_event()
        pending = usage_event(
            event_id=opaque("evt_h1_", "usage-two"),
            source_event_id=opaque("src_h1_", "source-two"),
            turn_key=opaque("turn_h1_", "turn-two"),
            total=18,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = root / "ledger"
            relative = ledger_relative_path(existing)
            target = ledger_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True)
            target.write_bytes(canonical(existing) + b"\n" + canonical(pending)[:23])

            before = LedgerReader(ledger_root).read_all()
            self.assertEqual(len(before.events), 1)
            self.assertEqual(before.issues[0].code, "partial_final_line_ignored")

            store = LocalStateStore(root / "state.sqlite")
            store.store_collection(source_cursor(), (pending,))
            flush = LedgerWriter(ledger_root, DEVICE_ID).flush(store)

            self.assertEqual(flush.partial_tails_recovered, 1)
            self.assertEqual(flush.appended, 1)
            after = LedgerReader(ledger_root).read_all()
            self.assertEqual(len(after.events), 2)
            self.assertEqual(after.issues, ())

    def test_complete_malformed_or_misrouted_line_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger_root = Path(directory) / "ledger"
            bad = ledger_root / "devices" / DEVICE_ID / "usage" / "2026" / "08"
            bad.mkdir(parents=True)
            (bad / "26.jsonl").write_bytes(b"{bad json}\n")
            with self.assertRaises(LedgerIoError):
                LedgerReader(ledger_root).read_all()

        with tempfile.TemporaryDirectory() as directory:
            ledger_root = Path(directory) / "ledger"
            wrong = ledger_root / "devices" / DEVICE_ID / "mappings" / "2026"
            wrong.mkdir(parents=True)
            (wrong / "08.jsonl").write_bytes(canonical(usage_event()) + b"\n")
            with self.assertRaises(LedgerIoError):
                LedgerReader(ledger_root).read_all()

    def test_conflict_cross_device_and_privacy_violation_remain_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_root = root / "ledger"
            event = usage_event()
            conflicting = usage_event(model="different-model")
            relative = ledger_relative_path(conflicting)
            target = ledger_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True)
            target.write_bytes(canonical(conflicting) + b"\n")
            store = LocalStateStore(root / "state.sqlite")
            store.store_collection(source_cursor(), (event,))

            with self.assertRaises(LedgerEventConflict):
                LedgerWriter(ledger_root, DEVICE_ID).flush(store)
            self.assertEqual(store.outbox_counts().pending, 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalStateStore(root / "state.sqlite")
            other_device = usage_event(
                device_id="00000000-0000-4000-8000-000000000002"
            )
            store.store_collection(source_cursor(), (other_device,))
            with self.assertRaises(LedgerIoError):
                LedgerWriter(root / "ledger", DEVICE_ID).flush(store)
            self.assertEqual(store.outbox_counts().pending, 1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalStateStore(root / "state.sqlite")
            raw_path = usage_event(model="C:\\private\\repository")
            store.store_collection(source_cursor(), (raw_path,))
            with self.assertRaises(PrivacyViolation):
                LedgerWriter(root / "ledger", DEVICE_ID).flush(store)
            self.assertEqual(store.outbox_counts().pending, 1)


if __name__ == "__main__":
    unittest.main()
