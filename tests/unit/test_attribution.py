from __future__ import annotations

import unittest

from codex_usage.domain.attribution import (
    ActivityProjectEvidence,
    ProjectResolutionKind,
    attribute_project,
)
from codex_usage.domain.git_remote import RemoteResolution, RemoteResolutionKind


def remote(
    kind: RemoteResolutionKind,
    canonical: str | None = None,
    *candidates: str,
) -> RemoteResolution:
    return RemoteResolution(kind, canonical, candidates)


class ProjectAttributionTests(unittest.TestCase):
    def test_manual_assignment_has_highest_priority(self) -> None:
        result = attribute_project(
            manual_project_id="manual-project",
            activity=(
                ActivityProjectEvidence(
                    "repo",
                    remote(RemoteResolutionKind.ORIGIN, "activity-project"),
                ),
            ),
            self_remote=remote(RemoteResolutionKind.ORIGIN, "self-project"),
        )

        self.assertEqual(result.project_identity, "manual-project")
        self.assertEqual(result.resolution, ProjectResolutionKind.MANUAL)

    def test_two_worktrees_of_same_remote_are_one_project(self) -> None:
        result = attribute_project(
            activity=(
                ActivityProjectEvidence(
                    "root-one",
                    remote(RemoteResolutionKind.ORIGIN, "github.com/a/repo"),
                ),
                ActivityProjectEvidence(
                    "root-two",
                    remote(RemoteResolutionKind.ORIGIN, "github.com/a/repo"),
                ),
            )
        )

        self.assertEqual(result.project_identity, "github.com/a/repo")
        self.assertEqual(result.resolution, ProjectResolutionKind.ACTIVITY_GIT)
        self.assertEqual(result.activity_repository_count, 2)

    def test_two_different_activity_repositories_are_ambiguous(self) -> None:
        result = attribute_project(
            activity=(
                ActivityProjectEvidence(
                    "root-a", remote(RemoteResolutionKind.ORIGIN, "project-a")
                ),
                ActivityProjectEvidence(
                    "root-b", remote(RemoteResolutionKind.ORIGIN, "project-b")
                ),
            )
        )

        self.assertIsNone(result.project_identity)
        self.assertEqual(
            result.resolution,
            ProjectResolutionKind.AMBIGUOUS_MULTI_REPO,
        )
        self.assertEqual(result.candidates, ("project-a", "project-b"))

    def test_one_ambiguous_remote_stops_instead_of_guessing_self(self) -> None:
        result = attribute_project(
            activity=(
                ActivityProjectEvidence(
                    "root",
                    remote(
                        RemoteResolutionKind.AMBIGUOUS_REMOTE,
                        None,
                        "candidate-a",
                        "candidate-b",
                    ),
                ),
            ),
            self_remote=remote(RemoteResolutionKind.ORIGIN, "self-project"),
        )

        self.assertIsNone(result.project_identity)
        self.assertEqual(result.resolution, ProjectResolutionKind.AMBIGUOUS_REMOTE)

    def test_self_origin_and_unique_remote_keep_distinct_reason(self) -> None:
        origin = attribute_project(
            self_remote=remote(RemoteResolutionKind.ORIGIN, "origin-project")
        )
        unique = attribute_project(
            self_remote=remote(RemoteResolutionKind.UNIQUE_REMOTE, "unique-project")
        )

        self.assertEqual(origin.resolution, ProjectResolutionKind.SELF_ORIGIN)
        self.assertEqual(unique.resolution, ProjectResolutionKind.UNIQUE_REMOTE)

    def test_lineage_priority_is_nearest_ancestor_then_root_then_descendant(self) -> None:
        ancestor = attribute_project(
            ancestor_projects=("nearest", "farther"),
            root_project="root",
            descendant_projects=("descendant",),
        )
        root = attribute_project(
            root_project="root",
            descendant_projects=("descendant",),
        )
        descendant = attribute_project(descendant_projects=("descendant",))

        self.assertEqual(ancestor.project_identity, "nearest")
        self.assertEqual(ancestor.resolution, ProjectResolutionKind.ANCESTOR)
        self.assertEqual(root.resolution, ProjectResolutionKind.ROOT)
        self.assertEqual(
            descendant.resolution,
            ProjectResolutionKind.DESCENDANT_CONSENSUS,
        )

    def test_local_mapping_and_unclassified_fallback(self) -> None:
        mapped_activity = attribute_project(
            activity=(
                ActivityProjectEvidence(
                    "local-root",
                    remote(RemoteResolutionKind.LOCAL_ONLY),
                    mapped_project_id="mapped-project",
                ),
            )
        )
        mapped_thread = attribute_project(local_project_id="mapped-thread")
        unknown = attribute_project(descendant_projects=("one", "two"))

        self.assertEqual(
            mapped_activity.resolution,
            ProjectResolutionKind.LOCAL_MAPPING,
        )
        self.assertEqual(mapped_thread.project_identity, "mapped-thread")
        self.assertEqual(unknown.resolution, ProjectResolutionKind.UNCLASSIFIED)
        self.assertEqual(unknown.candidates, ("one", "two"))

    def test_unresolved_second_activity_repository_blocks_partial_guess(self) -> None:
        result = attribute_project(
            activity=(
                ActivityProjectEvidence(
                    "known", remote(RemoteResolutionKind.ORIGIN, "project")
                ),
                ActivityProjectEvidence(
                    "unknown", remote(RemoteResolutionKind.LOCAL_ONLY)
                ),
            )
        )

        self.assertEqual(
            result.resolution,
            ProjectResolutionKind.AMBIGUOUS_MULTI_REPO,
        )


if __name__ == "__main__":
    unittest.main()
