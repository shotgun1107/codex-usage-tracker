from __future__ import annotations

import unittest

from codex_usage.domain.lineage import (
    ParentCandidate,
    ParentEvidenceSource,
    build_lineage,
)


class LineageTests(unittest.TestCase):
    def test_sqlite_parent_wins_and_ancestors_are_nearest_first(self) -> None:
        graph = build_lineage(
            {"root", "parent", "child"},
            (
                ParentCandidate(
                    "child", "wrong-parent", ParentEvidenceSource.FORK
                ),
                ParentCandidate(
                    "child", "parent", ParentEvidenceSource.SQLITE_SPAWN
                ),
                ParentCandidate(
                    "parent", "root", ParentEvidenceSource.SESSION_SOURCE
                ),
            ),
            {"child": "root", "parent": "root", "root": "root"},
        )

        self.assertEqual(graph.parent_of("child"), "parent")
        self.assertEqual(graph.ancestors_of("child"), ("parent", "root"))
        self.assertEqual(graph.root_of("child"), "root")
        self.assertEqual(graph.descendants_of("root"), ("parent", "child"))
        self.assertEqual(graph.issues[0].code, "lower_priority_parent_ignored")

    def test_equal_priority_conflict_is_not_inherited(self) -> None:
        graph = build_lineage(
            {"child", "one", "two"},
            (
                ParentCandidate("child", "one", ParentEvidenceSource.SQLITE_SPAWN),
                ParentCandidate("child", "two", ParentEvidenceSource.SQLITE_SPAWN),
            ),
        )

        self.assertIsNone(graph.parent_of("child"))
        self.assertEqual(graph.issues[0].code, "conflicting_parent_ignored")

    def test_cycle_and_self_parent_are_removed(self) -> None:
        graph = build_lineage(
            {"a", "b", "self"},
            (
                ParentCandidate("a", "b", ParentEvidenceSource.SQLITE_SPAWN),
                ParentCandidate("b", "a", ParentEvidenceSource.SQLITE_SPAWN),
                ParentCandidate("self", "self", ParentEvidenceSource.SQLITE_SPAWN),
            ),
        )

        self.assertEqual(dict(graph.parent_by_child), {})
        self.assertEqual(
            {issue.code for issue in graph.issues},
            {"lineage_cycle_ignored", "self_parent_ignored"},
        )

    def test_declared_root_is_available_without_parent_edge(self) -> None:
        graph = build_lineage({"child"}, (), {"child": "root"})

        self.assertEqual(graph.ancestors_of("child"), ())
        self.assertEqual(graph.root_of("child"), "root")
        with self.assertRaises(TypeError):
            graph.parent_by_child["child"] = "other"  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
