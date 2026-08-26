from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
import unittest

from codex_usage.application.project_attribution import (
    ProjectAttributionEngine,
    build_codex_lineage,
)
from codex_usage.domain.attribution import ProjectResolutionKind
from codex_usage.domain.git_remote import RemoteResolution, RemoteResolutionKind
from codex_usage.domain.token_usage import Operation, RawTokenCheckpoint, TokenCounts
from codex_usage.sources.codex_jsonl import RolloutMetadata
from codex_usage.sources.codex_sqlite import SpawnEdge, ThreadInventory
from codex_usage.sources.git import GitProbeResult, GitProbeStatus


COUNTS = TokenCounts(10, 0, None, 2, 0, 12)


def metadata(
    thread_id: str,
    *,
    root: str = "root",
    parent: str | None = None,
    remote: str | None = None,
    cwd: str | None = None,
) -> RolloutMetadata:
    return RolloutMetadata(
        thread_id=thread_id,
        root_session_id=root,
        forked_from_id=None,
        source_kind="subAgentThreadSpawn" if parent else "vscode",
        source_parent_thread_id=parent,
        cli_version="test",
        cwd=cwd,
        git_repository_url=remote,
        git_branch=None,
        git_commit_hash=None,
    )


def checkpoint(
    thread_id: str,
    turn_id: str,
    *workdirs: str,
) -> RawTokenCheckpoint:
    return RawTokenCheckpoint(
        rollout_thread_id=thread_id,
        rollout_forked_from_id=None,
        turn_id=turn_id,
        token_event_ordinal=0,
        record_index=2,
        occurred_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        operation=Operation.TURN,
        model="model",
        reasoning_effort="low",
        cumulative=COUNTS,
        reported_last=None,
        activity_workdirs=workdirs,
    )


def inventory(*edges: tuple[str, str]) -> ThreadInventory:
    return ThreadInventory(
        threads=MappingProxyType({}),
        spawn_edges=tuple(SpawnEdge(parent, child, None) for parent, child in edges),
        issues=(),
    )


class ProjectAttributionEngineTests(unittest.TestCase):
    def test_sqlite_lineage_overrides_conflicting_json_parent(self) -> None:
        metadata_by_thread = {
            "child": metadata("child", parent="json-parent"),
        }

        lineage = build_codex_lineage(
            metadata_by_thread,
            inventory(("sqlite-parent", "child")),
        )

        self.assertEqual(lineage.parent_of("child"), "sqlite-parent")
        self.assertEqual(lineage.issues[0].code, "lower_priority_parent_ignored")

    def test_activity_git_beats_orchestrator_location_and_child_uses_self(self) -> None:
        metadata_by_thread = {
            "root": metadata("root", root="root", cwd="outside"),
            "child": metadata(
                "child",
                root="root",
                parent="root",
                remote="https://github.com/example/child.git",
            ),
        }
        probes = {
            "outside": GitProbeResult(
                GitProbeStatus.NOT_REPOSITORY,
                None,
                RemoteResolution(RemoteResolutionKind.UNCLASSIFIED),
            ),
            "project-a-work": GitProbeResult(
                GitProbeStatus.REPOSITORY,
                "project-a-root",
                RemoteResolution(
                    RemoteResolutionKind.ORIGIN,
                    "github.com/example/project-a",
                    ("github.com/example/project-a",),
                ),
            ),
        }
        engine = ProjectAttributionEngine(
            metadata_by_thread,
            inventory(("root", "child")),
            git_probe=lambda path: probes[path],
        )

        results = engine.attribute_all(
            (
                checkpoint("root", "root-turn", "project-a-work"),
                checkpoint("child", "child-turn"),
            )
        )

        self.assertEqual(
            results[0].attribution.project_identity,
            "github.com/example/project-a",
        )
        self.assertEqual(
            results[0].attribution.resolution,
            ProjectResolutionKind.ACTIVITY_GIT,
        )
        self.assertEqual(
            results[1].attribution.project_identity,
            "github.com/example/child",
        )
        self.assertEqual(
            results[1].attribution.resolution,
            ProjectResolutionKind.SELF_ORIGIN,
        )

    def test_outside_orchestrator_inherits_single_descendant_consensus(self) -> None:
        metadata_by_thread = {
            "root": metadata("root", root="root", cwd="outside"),
            "child": metadata(
                "child",
                root="root",
                parent="root",
                remote="https://github.com/example/project.git",
            ),
        }
        not_repository = GitProbeResult(
            GitProbeStatus.NOT_REPOSITORY,
            None,
            RemoteResolution(RemoteResolutionKind.UNCLASSIFIED),
        )
        engine = ProjectAttributionEngine(
            metadata_by_thread,
            inventory(("root", "child")),
            git_probe=lambda _path: not_repository,
        )

        result = engine.attribute_all((checkpoint("root", "root-turn"),))[0]

        self.assertEqual(
            result.attribution.project_identity,
            "github.com/example/project",
        )
        self.assertEqual(
            result.attribution.resolution,
            ProjectResolutionKind.DESCENDANT_CONSENSUS,
        )

    def test_child_without_git_inherits_nearest_parent_project(self) -> None:
        metadata_by_thread = {
            "parent": metadata(
                "parent",
                root="parent",
                remote="https://github.com/example/project.git",
            ),
            "child": metadata("child", root="parent", parent="parent"),
        }
        engine = ProjectAttributionEngine(
            metadata_by_thread,
            inventory(("parent", "child")),
            git_probe=lambda _path: (_ for _ in ()).throw(AssertionError()),
        )

        result = engine.attribute_all((checkpoint("child", "turn"),))[0]

        self.assertEqual(result.attribution.resolution, ProjectResolutionKind.ANCESTOR)
        self.assertEqual(
            result.attribution.project_identity,
            "github.com/example/project",
        )

    def test_manual_turn_assignment_beats_all_automatic_evidence(self) -> None:
        metadata_by_thread = {
            "thread": metadata(
                "thread",
                root="thread",
                remote="https://github.com/example/self.git",
            )
        }
        engine = ProjectAttributionEngine(
            metadata_by_thread,
            inventory(),
            manual_turn_projects={"turn": "manual-project"},
        )

        result = engine.attribute_all((checkpoint("thread", "turn"),))[0]

        self.assertEqual(result.attribution.resolution, ProjectResolutionKind.MANUAL)
        self.assertEqual(result.attribution.project_identity, "manual-project")

    def test_local_session_remote_can_fall_back_to_cwd_git_probe(self) -> None:
        metadata_by_thread = {
            "thread": metadata(
                "thread",
                root="thread",
                remote="C:/local/repository.git",
                cwd="workdir",
            )
        }
        probe_result = GitProbeResult(
            GitProbeStatus.REPOSITORY,
            "repository-root",
            RemoteResolution(
                RemoteResolutionKind.UNIQUE_REMOTE,
                "github.com/example/project",
                ("github.com/example/project",),
            ),
        )
        engine = ProjectAttributionEngine(
            metadata_by_thread,
            inventory(),
            git_probe=lambda _path: probe_result,
        )

        result = engine.attribute_all((checkpoint("thread", "turn"),))[0]

        self.assertEqual(
            result.attribution.resolution,
            ProjectResolutionKind.UNIQUE_REMOTE,
        )
        self.assertEqual(
            result.attribution.project_identity,
            "github.com/example/project",
        )


if __name__ == "__main__":
    unittest.main()
