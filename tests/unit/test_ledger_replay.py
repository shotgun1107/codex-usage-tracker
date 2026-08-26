from __future__ import annotations

import unittest

from codex_usage.ledger.replay import (
    LedgerEventConflict,
    LedgerKeyMismatch,
    LedgerReplayError,
    RevisionChainError,
    replay_ledger_events,
)


def usage(
    event_id: str,
    source_event_id: str,
    *,
    revision: int = 1,
    supersedes: str | None = None,
    voided: bool = False,
    project_id: str | None = "project-original",
    thread_key: str = "thread-one",
    turn_key: str | None = "turn-one",
    occurred_at: str = "2026-08-26T01:00:00Z",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "usage_checkpoint",
        "event_id": event_id,
        "source_event_id": source_event_id,
        "revision": revision,
        "supersedes": supersedes,
        "voided": voided,
        "key_id": "key-one",
        "occurred_at": occurred_at,
        "project_id": project_id,
        "thread_key": thread_key,
        "turn_key": turn_key,
    }


def mapping(
    event_id: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    target_project_id: str | None,
    *,
    revision: int = 1,
    supersedes: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "mapping",
        "event_id": event_id,
        "revision": revision,
        "supersedes": supersedes,
        "key_id": "key-one",
        "occurred_at": "2026-08-26T02:00:00Z",
        "kind": kind,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "target_project_id": target_project_id,
    }


def quota(event_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_type": "quota_snapshot",
        "event_id": event_id,
        "key_id": "key-one",
        "occurred_at": "2026-08-26T03:00:00Z",
    }


class LedgerReplayTests(unittest.TestCase):
    def test_latest_revision_wins_and_voided_event_is_removed(self) -> None:
        events = (
            usage("old", "source", project_id="project-old"),
            usage(
                "new",
                "source",
                revision=2,
                supersedes="old",
                project_id="project-new",
            ),
            usage("void-old", "void-source"),
            usage(
                "void-new",
                "void-source",
                revision=2,
                supersedes="void-old",
                voided=True,
            ),
        )

        result = replay_ledger_events(reversed(events))

        self.assertEqual(result.input_event_count, 4)
        self.assertEqual(len(result.usage_events), 1)
        self.assertEqual(result.usage_events[0].payload["event_id"], "new")
        self.assertEqual(
            result.usage_events[0].effective_project_id,
            "project-new",
        )

    def test_exact_duplicate_is_idempotent_but_conflicts_fail(self) -> None:
        one = usage("event", "source")
        result = replay_ledger_events((one, dict(one)))
        self.assertEqual(result.input_event_count, 1)

        conflicting_id = dict(one, project_id="different")
        with self.assertRaises(LedgerEventConflict):
            replay_ledger_events((one, conflicting_id))

        conflicting_revision = usage("other-id", "source", project_id="different")
        with self.assertRaises(LedgerEventConflict):
            replay_ledger_events((one, conflicting_revision))

    def test_revision_gap_and_wrong_supersedes_fail(self) -> None:
        with self.assertRaises(RevisionChainError):
            replay_ledger_events(
                (
                    usage("one", "source"),
                    usage("three", "source", revision=3, supersedes="one"),
                )
            )
        with self.assertRaises(RevisionChainError):
            replay_ledger_events(
                (
                    usage("one", "source"),
                    usage("two", "source", revision=2, supersedes="wrong"),
                )
            )

    def test_turn_manual_beats_thread_manual_and_alias_is_resolved(self) -> None:
        result = replay_ledger_events(
            (
                usage("turn-event", "turn-source"),
                usage(
                    "thread-event",
                    "thread-source",
                    turn_key="another-turn",
                ),
                mapping(
                    "thread-map",
                    "manual_assignment",
                    "thread",
                    "thread-one",
                    "project-thread",
                ),
                mapping(
                    "turn-map",
                    "manual_assignment",
                    "turn",
                    "turn-one",
                    "project-turn",
                ),
                mapping(
                    "alias-one",
                    "project_alias",
                    "project",
                    "project-turn",
                    "project-middle",
                ),
                mapping(
                    "alias-two",
                    "project_alias",
                    "project",
                    "project-middle",
                    "project-final",
                ),
            )
        )

        by_event = {
            item.payload["event_id"]: item.effective_project_id
            for item in result.usage_events
        }
        self.assertEqual(by_event["turn-event"], "project-final")
        self.assertEqual(by_event["thread-event"], "project-thread")
        self.assertEqual(
            dict(result.project_aliases),
            {
                "project-turn": "project-final",
                "project-middle": "project-final",
            },
        )

    def test_alias_cycle_is_ignored_without_stopping_other_events(self) -> None:
        result = replay_ledger_events(
            (
                usage("usage", "source", project_id="project-a"),
                mapping(
                    "alias-a",
                    "project_alias",
                    "project",
                    "project-a",
                    "project-b",
                ),
                mapping(
                    "alias-b",
                    "project_alias",
                    "project",
                    "project-b",
                    "project-a",
                ),
            )
        )

        self.assertEqual(result.usage_events[0].effective_project_id, "project-a")
        self.assertEqual(dict(result.project_aliases), {})
        self.assertEqual(result.diagnostics[0].code, "project_alias_cycle_ignored")
        self.assertEqual(result.diagnostics[0].count, 2)

    def test_mapping_revision_can_clear_manual_assignment(self) -> None:
        result = replay_ledger_events(
            (
                usage("usage", "source"),
                mapping(
                    "map-old",
                    "manual_assignment",
                    "turn",
                    "turn-one",
                    "manual-project",
                ),
                mapping(
                    "map-new",
                    "manual_assignment",
                    "turn",
                    "turn-one",
                    None,
                    revision=2,
                    supersedes="map-old",
                ),
            )
        )

        self.assertEqual(
            result.usage_events[0].effective_project_id,
            "project-original",
        )
        self.assertEqual(result.mapping_events[0]["event_id"], "map-new")

    def test_key_mismatch_invalid_time_and_invalid_mapping_fail(self) -> None:
        second_key = dict(usage("two", "source-two"), key_id="key-two")
        with self.assertRaises(LedgerKeyMismatch):
            replay_ledger_events((usage("one", "source-one"), second_key))
        with self.assertRaises(LedgerKeyMismatch):
            replay_ledger_events(
                (usage("one", "source"),),
                expected_key_id="key-two",
            )
        with self.assertRaises(LedgerReplayError):
            replay_ledger_events(
                (usage("one", "source", occurred_at="2026-08-26T01:00:00+09:00"),)
            )
        with self.assertRaises(LedgerReplayError):
            replay_ledger_events(
                (
                    mapping(
                        "bad",
                        "manual_assignment",
                        "project",
                        "project",
                        "target",
                    ),
                )
            )

    def test_quota_and_output_order_are_deterministic(self) -> None:
        later = usage(
            "later",
            "source-later",
            occurred_at="2026-08-26T02:00:00Z",
        )
        earlier = usage(
            "earlier",
            "source-earlier",
            occurred_at="2026-08-26T01:00:00Z",
        )
        first = replay_ledger_events((quota("quota"), later, earlier))
        second = replay_ledger_events((earlier, quota("quota"), later))

        self.assertEqual(
            [item.payload["event_id"] for item in first.usage_events],
            ["earlier", "later"],
        )
        self.assertEqual(
            [item.payload["event_id"] for item in first.usage_events],
            [item.payload["event_id"] for item in second.usage_events],
        )
        self.assertEqual(len(first.quota_snapshots), 1)


if __name__ == "__main__":
    unittest.main()
