from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
import tempfile
import unittest

from codex_usage.ledger.replay import EffectiveUsageEvent, replay_ledger_events
from codex_usage.storage.sqlite import (
    LocalStateStore,
    LocalStoreError,
    ReadModelCounts,
    SourceCursor,
)


def token_counts(total: int) -> dict[str, int | None]:
    return {
        "input_tokens": total - 2,
        "cached_input_tokens": 1,
        "cache_write_input_tokens": None,
        "output_tokens": 2,
        "reasoning_output_tokens": 1,
        "total_tokens": total,
    }


def usage_event(
    event_id: str,
    source_event_id: str,
    turn_key: str,
    total: int,
    *,
    delta: dict[str, int | None] | None = None,
) -> dict[str, object]:
    counts = token_counts(total)
    return {
        "schema_version": 1,
        "event_type": "usage_checkpoint",
        "event_id": event_id,
        "source_event_id": source_event_id,
        "revision": 1,
        "supersedes": None,
        "voided": False,
        "parser_version": "0.1.0",
        "device_id": "00000000-0000-4000-8000-000000000001",
        "key_id": "key_h1_example",
        "project_id": "project-original",
        "project_resolution": "self_origin",
        "activity_repository_count": 0,
        "thread_key": "thread-key",
        "root_thread_key": "thread-key",
        "parent_thread_key": None,
        "forked_from_thread_key": None,
        "turn_key": turn_key,
        "token_event_ordinal": 0,
        "operation": "turn",
        "occurred_at": "2026-08-26T05:00:00Z",
        "model": "model-one",
        "reasoning_effort": "medium",
        "source_kind": "vscode",
        "cli_version": "0.150.0",
        "cumulative": counts,
        "delta": counts if delta is None else delta,
        "reported_last": counts,
        "flags": [],
    }


def mapping_event(
    event_id: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    target_project_id: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "mapping",
        "event_id": event_id,
        "revision": 1,
        "supersedes": None,
        "device_id": "00000000-0000-4000-8000-000000000001",
        "key_id": "key_h1_example",
        "occurred_at": "2026-08-26T06:00:00Z",
        "kind": kind,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "target_project_id": target_project_id,
        "display_value": None,
    }


def quota_event() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "quota_snapshot",
        "event_id": "quota-event",
        "device_id": "00000000-0000-4000-8000-000000000001",
        "key_id": "key_h1_example",
        "occurred_at": "2026-08-26T07:00:00Z",
        "scope_key": None,
        "window_minutes": 300,
        "used_percent": 20.0,
        "remaining_percent": 80.0,
        "reset_at": "2026-08-26T12:00:00Z",
    }


class ReadModelIntegrationTests(unittest.TestCase):
    def test_rebuild_is_deterministic_and_preserves_operational_state(self) -> None:
        events = (
            usage_event("usage-one", "source-one", "turn-one", 12),
            usage_event("usage-two", "source-two", "turn-two", 18),
            mapping_event(
                "manual-map",
                "manual_assignment",
                "thread",
                "thread-key",
                "project-manual",
            ),
            mapping_event(
                "alias-map",
                "project_alias",
                "project",
                "project-manual",
                "project-final",
            ),
            quota_event(),
        )
        replay = replay_ledger_events(events, expected_key_id="key_h1_example")

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            source_cursor = SourceCursor(
                source_id="local-source",
                source_path="C:/local/source.jsonl",
                fingerprint="fingerprint",
                byte_offset=100,
                last_complete_line_digest="digest",
            )
            store.store_collection(source_cursor, (events[0],))

            first_state = store.rebuild_read_model(replay)

            self.assertEqual(first_state.generation, 1)
            self.assertEqual(first_state.input_event_count, 5)
            self.assertEqual(first_state.effective_usage_count, 2)
            self.assertEqual(store.get_cursor("local-source"), source_cursor)
            self.assertEqual(store.outbox_counts().pending, 1)
            self.assertEqual(
                store.read_model_counts(),
                ReadModelCounts(usage=2, mappings=2, aliases=1, quota=1),
            )
            daily = store.daily_usage_utc()
            self.assertEqual(len(daily), 1)
            self.assertEqual(daily[0].utc_date, "2026-08-26")
            self.assertEqual(daily[0].effective_project_id, "project-final")
            self.assertEqual(daily[0].total_tokens, 30)
            self.assertEqual(daily[0].event_count, 2)

            second_replay = replay_ledger_events(reversed(events))
            second_state = store.rebuild_read_model(second_replay)

            self.assertEqual(second_state.generation, 2)
            self.assertEqual(store.read_model_counts().usage, 2)
            self.assertEqual(store.daily_usage_utc()[0].total_tokens, 30)

            third_state = store.rebuild_read_model(replay)
            self.assertEqual(third_state.generation, 3)
            self.assertEqual(store.daily_usage_utc()[0].total_tokens, 30)

    def test_failed_rebuild_rolls_back_to_previous_generation(self) -> None:
        valid_event = usage_event("usage", "source", "turn", 12)
        replay = replay_ledger_events((valid_event,))

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.rebuild_read_model(replay)
            broken_payload = dict(replay.usage_events[0].payload)
            broken_payload.pop("device_id")
            broken_replay = replace(
                replay,
                usage_events=(
                    EffectiveUsageEvent(
                        MappingProxyType(broken_payload),
                        replay.usage_events[0].effective_project_id,
                    ),
                ),
            )

            with self.assertRaises(LocalStoreError):
                store.rebuild_read_model(broken_replay)

            self.assertEqual(store.read_model_state().generation, 1)  # type: ignore[union-attr]
            self.assertEqual(store.read_model_counts().usage, 1)
            self.assertEqual(store.daily_usage_utc()[0].total_tokens, 12)

    def test_null_delta_is_retained_but_excluded_from_daily_sum(self) -> None:
        invalid_delta = usage_event(
            "usage",
            "source",
            "turn",
            12,
            delta=None,
        )
        invalid_delta["delta"] = None
        replay = replay_ledger_events((invalid_delta,))

        with tempfile.TemporaryDirectory() as directory:
            store = LocalStateStore(Path(directory) / "state.sqlite")
            store.rebuild_read_model(replay)

            self.assertEqual(store.read_model_counts().usage, 1)
            self.assertEqual(store.daily_usage_utc(), ())


if __name__ == "__main__":
    unittest.main()
