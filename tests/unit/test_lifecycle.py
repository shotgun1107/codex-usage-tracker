from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import unittest

from codex_usage.domain.lifecycle import (
    DuplicateCheckpointConflict,
    calculate_deltas,
    deduplicate_events,
)
from codex_usage.domain.token_usage import (
    CalculatedTokenEvent,
    Operation,
    RawTokenCheckpoint,
    TokenCounts,
    TokenDataError,
)
from codex_usage.sources.codex_jsonl import parse_rollout


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle"
UTC_NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


def fixture_events(name: str) -> list[CalculatedTokenEvent]:
    lines = (FIXTURE_ROOT / name).read_text(encoding="utf-8").splitlines()
    return calculate_deltas(parse_rollout(lines).checkpoints)


def counts(input_tokens: int, output_tokens: int, total_tokens: int) -> TokenCounts:
    return TokenCounts(
        input_tokens=input_tokens,
        cached_input_tokens=0,
        cache_write_input_tokens=None,
        output_tokens=output_tokens,
        reasoning_output_tokens=0,
        total_tokens=total_tokens,
    )


def checkpoint(
    *,
    thread_id: str,
    turn_id: str,
    cumulative: TokenCounts,
    reported_last: TokenCounts | None = None,
    forked_from_id: str | None = None,
    record_index: int = 1,
) -> RawTokenCheckpoint:
    return RawTokenCheckpoint(
        rollout_thread_id=thread_id,
        rollout_forked_from_id=forked_from_id,
        turn_id=turn_id,
        token_event_ordinal=0,
        record_index=record_index,
        occurred_at=UTC_NOW,
        operation=Operation.TURN,
        model="model",
        reasoning_effort="low",
        cumulative=cumulative,
        reported_last=reported_last,
    )


class TokenCountsTests(unittest.TestCase):
    def test_invalid_token_values_are_rejected(self) -> None:
        with self.assertRaises(TokenDataError):
            counts(-1, 1, 0)
        with self.assertRaises(TokenDataError):
            TokenCounts.from_mapping({"total_tokens": True})

    def test_total_consistency_handles_complete_and_partial_counts(self) -> None:
        self.assertTrue(counts(9, 1, 10).is_total_consistent())
        self.assertFalse(counts(9, 1, 11).is_total_consistent())
        partial = TokenCounts.from_mapping({"total_tokens": 10})
        self.assertIsNone(partial.is_total_consistent())


class LifecycleTests(unittest.TestCase):
    def test_new_resume_compact_and_post_compact_deltas(self) -> None:
        events = fixture_events("parent.jsonl")

        self.assertEqual(
            [event.delta.total_tokens for event in events if event.delta],
            [100, 70, 0, 60],
        )
        self.assertEqual(events[2].checkpoint.operation, Operation.COMPACT)
        self.assertIn("compact_reported_last_excluded", events[2].flags)
        self.assertNotIn("reported_last_mismatch", events[2].flags)
        self.assertIsNone(events[1].delta.cache_write_input_tokens)

    def test_incremental_calculation_accepts_previous_cumulative(self) -> None:
        parsed = parse_rollout(
            (FIXTURE_ROOT / "parent.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )

        events = calculate_deltas(
            [parsed.checkpoints[-1]],
            previous_cumulative=parsed.checkpoints[-2].cumulative,
        )

        self.assertEqual(events[0].delta.total_tokens, 60)

    def test_fork_copies_are_deduplicated_and_new_usage_is_retained(self) -> None:
        parent_events = fixture_events("parent.jsonl")
        fork_events = fixture_events("fork.jsonl")

        self.assertEqual(
            [event.delta.total_tokens for event in fork_events if event.delta],
            [100, 70, 90],
        )

        deduplicated = deduplicate_events([*fork_events, *parent_events])

        self.assertEqual(len(deduplicated), 5)
        self.assertEqual(
            sum(event.delta.total_tokens for event in deduplicated if event.delta),
            320,
        )
        selected_turn_one = next(
            event for event in deduplicated if event.checkpoint.turn_id == "turn-one"
        )
        self.assertEqual(
            selected_turn_one.checkpoint.rollout_thread_id,
            "parent-thread",
        )

    def test_total_regression_is_quarantined_and_next_baseline_can_continue(self) -> None:
        previous = checkpoint(
            thread_id="thread",
            turn_id="one",
            cumulative=counts(100, 10, 110),
        )
        regressed = checkpoint(
            thread_id="thread",
            turn_id="two",
            cumulative=counts(90, 10, 100),
            record_index=2,
        )
        recovered = checkpoint(
            thread_id="thread",
            turn_id="three",
            cumulative=counts(110, 10, 120),
            record_index=3,
        )

        events = calculate_deltas([previous, regressed, recovered])

        self.assertIsNone(events[1].delta)
        self.assertIn("counter_regression", events[1].flags)
        self.assertEqual(events[2].delta.total_tokens, 20)

    def test_component_regression_nulls_only_that_component(self) -> None:
        previous = counts(100, 10, 110)
        current = counts(90, 30, 120)
        event = calculate_deltas(
            [
                checkpoint(
                    thread_id="thread",
                    turn_id="turn",
                    cumulative=current,
                )
            ],
            previous_cumulative=previous,
        )[0]

        self.assertIsNotNone(event.delta)
        self.assertIsNone(event.delta.input_tokens)
        self.assertEqual(event.delta.output_tokens, 20)
        self.assertEqual(event.delta.total_tokens, 10)
        self.assertIn("component_regression:input_tokens", event.flags)

    def test_positive_reported_last_mismatch_is_flagged(self) -> None:
        event = calculate_deltas(
            [
                checkpoint(
                    thread_id="thread",
                    turn_id="turn",
                    cumulative=counts(90, 10, 100),
                    reported_last=counts(80, 10, 90),
                )
            ]
        )[0]

        self.assertIn("reported_last_mismatch", event.flags)

    def test_missing_turn_id_is_marked_with_weak_dedupe_key(self) -> None:
        event = calculate_deltas(
            [
                replace(
                    checkpoint(
                        thread_id="legacy-thread",
                        turn_id="placeholder",
                        cumulative=counts(9, 1, 10),
                    ),
                    turn_id=None,
                )
            ]
        )[0]

        self.assertIn("weak_dedupe_key", event.flags)

    def test_conflicting_duplicate_logical_events_fail_closed(self) -> None:
        original = calculate_deltas(
            [
                checkpoint(
                    thread_id="parent",
                    turn_id="same-turn",
                    cumulative=counts(90, 10, 100),
                )
            ]
        )[0]
        conflicting_checkpoint = replace(
            original.checkpoint,
            rollout_thread_id="child",
            rollout_forked_from_id="parent",
            cumulative=counts(100, 10, 110),
        )
        conflicting = replace(
            original,
            checkpoint=conflicting_checkpoint,
            delta=counts(100, 10, 110),
        )

        with self.assertRaises(DuplicateCheckpointConflict):
            deduplicate_events([original, conflicting])

    def test_duplicate_can_enrich_missing_model_metadata(self) -> None:
        parent = calculate_deltas(
            [
                replace(
                    checkpoint(
                        thread_id="parent",
                        turn_id="same-turn",
                        cumulative=counts(9, 1, 10),
                    ),
                    model=None,
                    reasoning_effort=None,
                )
            ]
        )[0]
        child = replace(
            parent,
            checkpoint=replace(
                parent.checkpoint,
                rollout_thread_id="child",
                rollout_forked_from_id="parent",
                model="model",
                reasoning_effort="low",
            ),
        )

        deduplicated = deduplicate_events([child, parent])

        self.assertEqual(len(deduplicated), 1)
        self.assertEqual(deduplicated[0].checkpoint.rollout_thread_id, "parent")
        self.assertEqual(deduplicated[0].checkpoint.model, "model")
        self.assertEqual(deduplicated[0].checkpoint.reasoning_effort, "low")

    def test_conflicting_optional_metadata_is_nulled_and_flagged(self) -> None:
        first = calculate_deltas(
            [
                checkpoint(
                    thread_id="parent",
                    turn_id="same-turn",
                    cumulative=counts(9, 1, 10),
                )
            ]
        )[0]
        second = replace(
            first,
            checkpoint=replace(
                first.checkpoint,
                rollout_thread_id="child",
                rollout_forked_from_id="parent",
                model="different-model",
                reasoning_effort="high",
            ),
        )

        deduplicated = deduplicate_events([first, second])

        self.assertEqual(len(deduplicated), 1)
        self.assertIsNone(deduplicated[0].checkpoint.model)
        self.assertIsNone(deduplicated[0].checkpoint.reasoning_effort)
        self.assertIn("metadata_conflict:model", deduplicated[0].flags)
        self.assertIn(
            "metadata_conflict:reasoning_effort",
            deduplicated[0].flags,
        )

    def test_fork_ancestry_cycle_fails_closed(self) -> None:
        first = calculate_deltas(
            [
                checkpoint(
                    thread_id="first",
                    forked_from_id="second",
                    turn_id="first-turn",
                    cumulative=counts(9, 1, 10),
                )
            ]
        )[0]
        second = calculate_deltas(
            [
                checkpoint(
                    thread_id="second",
                    forked_from_id="first",
                    turn_id="second-turn",
                    cumulative=counts(9, 1, 10),
                )
            ]
        )[0]

        with self.assertRaises(DuplicateCheckpointConflict):
            deduplicate_events([first, second])


if __name__ == "__main__":
    unittest.main()
