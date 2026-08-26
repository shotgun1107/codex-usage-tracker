from __future__ import annotations

import json
from pathlib import Path
import unittest

from codex_usage.domain.token_usage import Operation
from codex_usage.sources.codex_jsonl import RolloutParseError, parse_rollout


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle"


def load_fixture(name: str) -> list[str]:
    return (FIXTURE_ROOT / name).read_text(encoding="utf-8").splitlines()


def encode(record: dict[str, object]) -> str:
    return json.dumps(record, separators=(",", ":"))


class CodexJsonlParserTests(unittest.TestCase):
    def test_parent_fixture_parses_metadata_turns_models_and_compact(self) -> None:
        result = parse_rollout(load_fixture("parent.jsonl"))

        self.assertEqual(result.metadata.thread_id, "parent-thread")
        self.assertEqual(result.metadata.root_session_id, "parent-thread")
        self.assertEqual(result.metadata.source_kind, "exec")
        self.assertEqual(result.metadata.cli_version, "0.150.0-alpha.8")
        self.assertEqual(
            result.metadata.git_repository_url,
            "https://github.com/example/project.git",
        )
        self.assertEqual(len(result.checkpoints), 4)
        self.assertEqual(
            [checkpoint.turn_id for checkpoint in result.checkpoints],
            ["turn-one", "turn-two", "turn-compact", "turn-three"],
        )
        self.assertEqual(
            [checkpoint.operation for checkpoint in result.checkpoints],
            [Operation.TURN, Operation.TURN, Operation.COMPACT, Operation.TURN],
        )
        self.assertEqual(result.checkpoints[1].model, "gpt-test-2")
        self.assertEqual(result.checkpoints[1].reasoning_effort, "medium")
        self.assertIsNone(
            result.checkpoints[1].cumulative.cache_write_input_tokens
        )
        self.assertEqual(result.issues, ())

    def test_fork_uses_first_session_meta_and_keeps_copied_turn_ids(self) -> None:
        result = parse_rollout(load_fixture("fork.jsonl"))

        self.assertEqual(result.metadata.thread_id, "fork-thread")
        self.assertEqual(result.metadata.forked_from_id, "parent-thread")
        self.assertEqual(
            [checkpoint.turn_id for checkpoint in result.checkpoints],
            ["turn-one", "turn-two", "fork-turn"],
        )
        self.assertTrue(
            all(
                checkpoint.rollout_thread_id == "fork-thread"
                for checkpoint in result.checkpoints
            )
        )

    def test_subagent_source_is_normalized_and_parent_is_extracted(self) -> None:
        source = {
            "subagent": {
                "thread_spawn": {
                    "parent_thread_id": "parent-thread",
                    "depth": 1,
                }
            }
        }
        lines = [
            encode(
                {
                    "timestamp": "2026-08-26T01:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "child-thread",
                        "session_id": "root-thread",
                        "source": source,
                    },
                }
            )
        ]

        result = parse_rollout(lines)

        self.assertEqual(result.metadata.source_kind, "subAgentThreadSpawn")
        self.assertEqual(result.metadata.source_parent_thread_id, "parent-thread")
        self.assertEqual(result.metadata.root_session_id, "root-thread")

    def test_multiple_checkpoints_in_one_turn_receive_stable_ordinals(self) -> None:
        counts = (
            {
                "input_tokens": 9,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "reasoning_output_tokens": 0,
                "total_tokens": 10,
            },
            {
                "input_tokens": 22,
                "cached_input_tokens": 5,
                "output_tokens": 3,
                "reasoning_output_tokens": 1,
                "total_tokens": 25,
            },
        )
        lines = [
            encode(
                {
                    "timestamp": "2026-08-26T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread", "source": "cli"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:01Z",
                    "type": "turn_context",
                    "payload": {"turn_id": "turn", "model": "model"},
                }
            ),
            *[
                encode(
                    {
                        "timestamp": f"2026-08-26T01:00:0{index + 2}Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "token_count",
                            "info": {"total_token_usage": value},
                        },
                    }
                )
                for index, value in enumerate(counts)
            ],
        ]
        lines.insert(
            4,
            encode(
                {
                    "timestamp": "2026-08-26T01:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "custom_tool_call",
                        "name": "exec",
                        "input": (
                            "const result = await tools.exec_command({\n"
                            '  cmd: "git status",\n'
                            '  workdir: "C:\\\\repo-a"\n'
                            "});"
                        ),
                    },
                }
            ),
        )
        lines.append(
            encode(
                {
                    "timestamp": "2026-08-26T01:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {"options": {"workdir": "C:/repo-b"}}
                        ),
                    },
                }
            )
        )

        result = parse_rollout(lines)

        self.assertEqual(
            [checkpoint.token_event_ordinal for checkpoint in result.checkpoints],
            [0, 1],
        )
        self.assertEqual(
            result.checkpoints[0].activity_workdirs,
            ("C:/repo-b", "C:\\repo-a"),
        )
        self.assertEqual(
            result.checkpoints[1].activity_workdirs,
            ("C:/repo-b", "C:\\repo-a"),
        )

    def test_token_without_info_is_skipped_with_issue(self) -> None:
        lines = [
            encode(
                {
                    "timestamp": "2026-08-26T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": None},
                }
            ),
        ]

        result = parse_rollout(lines)

        self.assertEqual(result.checkpoints, ())
        self.assertEqual(result.issues[0].code, "token_count_without_info")

    def test_token_without_turn_uses_record_fallback_key_and_issue(self) -> None:
        lines = [
            encode(
                {
                    "timestamp": "2026-08-26T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 9,
                                "output_tokens": 1,
                                "total_tokens": 10,
                            }
                        },
                    },
                }
            ),
        ]

        result = parse_rollout(lines)

        checkpoint = result.checkpoints[0]
        self.assertIsNone(checkpoint.turn_id)
        self.assertEqual(checkpoint.logical_key, ("record", "thread", 2))
        self.assertEqual(result.issues[0].code, "token_count_without_turn")

    def test_invalid_json_missing_metadata_and_naive_timestamp_fail_closed(self) -> None:
        with self.assertRaises(RolloutParseError):
            parse_rollout(["{"])
        with self.assertRaises(RolloutParseError):
            parse_rollout(
                [
                    encode(
                        {
                            "timestamp": "2026-08-26T01:00:00Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message"},
                        }
                    )
                ]
            )

        lines = [line.replace("01:00:02Z", "01:00:02") for line in load_fixture("parent.jsonl")]
        with self.assertRaises(RolloutParseError):
            parse_rollout(lines)

    def test_missing_cumulative_total_fails_closed(self) -> None:
        lines = [
            encode(
                {
                    "timestamp": "2026-08-26T01:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "thread"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:01Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn"},
                }
            ),
            encode(
                {
                    "timestamp": "2026-08-26T01:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 10}},
                    },
                }
            ),
        ]

        with self.assertRaises(RolloutParseError):
            parse_rollout(lines)


if __name__ == "__main__":
    unittest.main()
