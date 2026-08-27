from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_usage.sources.rollout_files import (
    PreviousRolloutCursor,
    discover_rollout_files,
    read_changed_rollout,
    rollout_source_id,
)


class RolloutFileTests(unittest.TestCase):
    def test_discovery_prefers_active_tree_and_includes_archived(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            active = root / "sessions" / "2026" / "active.jsonl"
            archived = root / "archived_sessions" / "archived.jsonl"
            active.parent.mkdir(parents=True)
            archived.parent.mkdir(parents=True)
            active.write_text("{}\n", encoding="utf-8")
            archived.write_text("{}\n", encoding="utf-8")

            files = discover_rollout_files(root)

            self.assertEqual(files, (active.resolve(), archived.resolve()))

    def test_complete_lines_partial_tail_and_unchanged_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_bytes("첫째\n둘째\n부분".encode("utf-8"))

            first = read_changed_rollout(path, None)
            assert first is not None

            self.assertEqual(first.lines, ("첫째", "둘째"))
            self.assertEqual(first.byte_offset, len("첫째\n둘째\n".encode("utf-8")))
            self.assertIsNotNone(first.last_complete_line_digest)
            self.assertIsNone(
                read_changed_rollout(
                    path,
                    PreviousRolloutCursor(first.fingerprint, first.byte_offset),
                )
            )

            with path.open("ab") as output:
                output.write(" 완료\n".encode("utf-8"))
            second = read_changed_rollout(
                path,
                PreviousRolloutCursor(first.fingerprint, first.byte_offset),
            )
            assert second is not None
            self.assertEqual(second.lines[-1], "부분 완료")
            self.assertGreater(second.byte_offset, first.byte_offset)

    def test_truncation_and_replacement_are_returned_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("one\ntwo\n", encoding="utf-8")
            first = read_changed_rollout(path, None)
            assert first is not None

            path.write_text("new\n", encoding="utf-8")
            replacement = read_changed_rollout(
                path,
                PreviousRolloutCursor(first.fingerprint, first.byte_offset),
            )

            assert replacement is not None
            self.assertEqual(replacement.lines, ("new",))
            self.assertLess(replacement.byte_offset, first.byte_offset)

    def test_source_id_is_stable_and_path_specific(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = root / "one.jsonl"
            two = root / "two.jsonl"

            self.assertEqual(rollout_source_id(one), rollout_source_id(one))
            self.assertNotEqual(rollout_source_id(one), rollout_source_id(two))

    def test_snapshot_attempt_count_must_be_positive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_changed_rollout(path, None, stability_attempts=0)


if __name__ == "__main__":
    unittest.main()
