from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from codex_usage.application.collect import (
    CollectError,
    CollectService,
    find_codex_state_database,
)
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerReader
from codex_usage.privacy.identifiers import generate_shared_key, key_id
from codex_usage.storage.sqlite import LocalStateStore


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle" / "parent.jsonl"
DEVICE_ID = "00000000-0000-4000-8000-000000000001"


def make_config(root: Path, shared_key: bytes) -> AppConfig:
    codex_home = root / "codex-home"
    (codex_home / "sessions" / "2026").mkdir(parents=True)
    return AppConfig(
        schema_version=1,
        device_id=DEVICE_ID,
        codex_home=str(codex_home.resolve()),
        state_db=str((root / "local-state.sqlite").resolve()),
        ledger_root=str((root / "ledger").resolve()),
        credential_target=f"CodexUsageTracker/{key_id(shared_key)}",
        key_id=key_id(shared_key),
    )


def append_turn(path: Path) -> None:
    records = (
        {
            "timestamp": "2026-08-26T01:00:14Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-four"},
        },
        {
            "timestamp": "2026-08-26T01:00:14Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-four",
                "model": "gpt-test-2",
                "effort": "medium",
            },
        },
        {
            "timestamp": "2026-08-26T01:00:15Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 225,
                        "cached_input_tokens": 55,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 35,
                        "reasoning_output_tokens": 6,
                        "total_tokens": 260,
                    },
                    "last_token_usage": {
                        "input_tokens": 25,
                        "cached_input_tokens": 5,
                        "cache_write_input_tokens": 0,
                        "output_tokens": 5,
                        "reasoning_output_tokens": 1,
                        "total_tokens": 30,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-26T01:00:16Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "turn-four"},
        },
    )
    with path.open("a", encoding="utf-8", newline="\n") as output:
        for record in records:
            output.write(json.dumps(record, separators=(",", ":")) + "\n")


class CollectServiceIntegrationTests(unittest.TestCase):
    def test_first_repeat_and_appended_turn_collection(self) -> None:
        shared_key = generate_shared_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root, shared_key)
            rollout = Path(config.codex_home) / "sessions" / "2026" / "rollout.jsonl"
            rollout.write_bytes(FIXTURE.read_bytes())
            service = CollectService(config, shared_key)

            first = service.collect()

            self.assertEqual(first.discovered_files, 1)
            self.assertEqual(first.changed_files, 1)
            self.assertEqual(first.parsed_files, 1)
            self.assertEqual(first.new_usage_events, 4)
            self.assertEqual(first.flushed.appended, 4)
            self.assertEqual(first.ledger_event_count, 4)
            self.assertFalse(first.sqlite_lineage_available)
            store = LocalStateStore(config.state_db)
            self.assertEqual(store.read_model_counts().usage, 4)
            self.assertEqual(
                sum(row.total_tokens or 0 for row in store.daily_usage_utc()),
                230,
            )

            second = service.collect()

            self.assertEqual(second.changed_files, 0)
            self.assertEqual(second.new_usage_events, 0)
            self.assertEqual(second.flushed.pending_seen, 0)
            self.assertEqual(second.ledger_event_count, 4)

            append_turn(rollout)
            third = service.collect()

            self.assertEqual(third.changed_files, 1)
            self.assertEqual(third.calculated_checkpoints, 5)
            self.assertEqual(third.existing_usage_events, 4)
            self.assertEqual(third.new_usage_events, 1)
            self.assertEqual(third.ledger_event_count, 5)
            self.assertEqual(store.read_model_counts().usage, 5)
            self.assertEqual(
                sum(row.total_tokens or 0 for row in store.daily_usage_utc()),
                260,
            )

            ledger_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(config.ledger_root).rglob("*.jsonl")
            )
            self.assertNotIn("C:/fixture/project", ledger_text)
            self.assertNotIn("github.com/example/project", ledger_text)
            self.assertEqual(len(LedgerReader(config.ledger_root).read_all().events), 5)

    def test_invalid_complete_rollout_is_quarantined_without_advancing_cursor(self) -> None:
        shared_key = generate_shared_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root, shared_key)
            rollout = Path(config.codex_home) / "sessions" / "2026" / "bad.jsonl"
            rollout.write_text("{invalid}\n", encoding="utf-8")
            service = CollectService(config, shared_key)

            result = service.collect()

            store = LocalStateStore(config.state_db)
            self.assertEqual(result.invalid_files, 1)
            self.assertEqual(dict(store.all_cursors()), {})
            self.assertEqual(store.outbox_counts().pending, 0)

    def test_key_mismatch_and_state_database_selection(self) -> None:
        first_key = generate_shared_key()
        second_key = generate_shared_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = make_config(root, first_key)
            with self.assertRaises(CollectError):
                CollectService(config, second_key)

            codex_home = Path(config.codex_home)
            (codex_home / "state_2.sqlite").touch()
            (codex_home / "state_10.sqlite").touch()
            self.assertEqual(
                find_codex_state_database(codex_home),
                codex_home / "state_10.sqlite",
            )


if __name__ == "__main__":
    unittest.main()
