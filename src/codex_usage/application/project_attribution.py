"""Join rollout, SQLite, Git, and lineage evidence for turn attribution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from codex_usage.domain.attribution import (
    ActivityProjectEvidence,
    ProjectAttribution,
    attribute_project,
)
from codex_usage.domain.git_remote import (
    RemoteNormalizationError,
    RemoteResolution,
    RemoteResolutionKind,
    resolve_remote,
)
from codex_usage.domain.lineage import (
    LineageGraph,
    ParentCandidate,
    ParentEvidenceSource,
    build_lineage,
)
from codex_usage.domain.token_usage import RawTokenCheckpoint
from codex_usage.sources.codex_jsonl import RolloutMetadata
from codex_usage.sources.codex_sqlite import ThreadInventory
from codex_usage.sources.git import (
    GitProbeError,
    GitProbeResult,
    GitProbeStatus,
    probe_git_repository,
)


@dataclass(frozen=True, slots=True)
class AttributedCheckpoint:
    """One raw token checkpoint paired with its local project decision."""

    checkpoint: RawTokenCheckpoint
    attribution: ProjectAttribution


def build_codex_lineage(
    metadata_by_thread: Mapping[str, RolloutMetadata],
    inventory: ThreadInventory,
) -> LineageGraph:
    """Combine SQLite spawn edges with JSONL parent and fork evidence."""

    thread_ids = set(inventory.threads) | set(metadata_by_thread)
    candidates: list[ParentCandidate] = [
        ParentCandidate(
            edge.child_thread_id,
            edge.parent_thread_id,
            ParentEvidenceSource.SQLITE_SPAWN,
        )
        for edge in inventory.spawn_edges
    ]
    declared_roots: dict[str, str] = {}

    for metadata in metadata_by_thread.values():
        declared_roots[metadata.thread_id] = metadata.root_session_id
        if metadata.source_parent_thread_id is not None:
            candidates.append(
                ParentCandidate(
                    metadata.thread_id,
                    metadata.source_parent_thread_id,
                    ParentEvidenceSource.SESSION_SOURCE,
                )
            )
        if metadata.forked_from_id is not None:
            candidates.append(
                ParentCandidate(
                    metadata.thread_id,
                    metadata.forked_from_id,
                    ParentEvidenceSource.FORK,
                )
            )

    return build_lineage(thread_ids, candidates, declared_roots)


class ProjectAttributionEngine:
    """Resolve every checkpoint without exposing paths or remotes in output."""

    def __init__(
        self,
        metadata_by_thread: Mapping[str, RolloutMetadata],
        inventory: ThreadInventory,
        *,
        lineage: LineageGraph | None = None,
        manual_turn_projects: Mapping[str, str] | None = None,
        manual_thread_projects: Mapping[str, str] | None = None,
        local_repository_projects: Mapping[str, str] | None = None,
        git_probe: Callable[[str], GitProbeResult] = probe_git_repository,
    ) -> None:
        self._metadata = dict(metadata_by_thread)
        self._inventory = inventory
        self._lineage = lineage or build_codex_lineage(
            self._metadata,
            inventory,
        )
        self._manual_turn_projects = dict(manual_turn_projects or {})
        self._manual_thread_projects = dict(manual_thread_projects or {})
        self._local_repository_projects = dict(local_repository_projects or {})
        self._git_probe = git_probe
        self._probe_cache: dict[str, GitProbeResult | None] = {}
        self._self_remote_cache: dict[str, RemoteResolution] = {}

    @property
    def lineage(self) -> LineageGraph:
        return self._lineage

    def attribute_all(
        self,
        checkpoints: Iterable[RawTokenCheckpoint],
    ) -> tuple[AttributedCheckpoint, ...]:
        """Resolve a stable batch, including descendant consensus hints."""

        checkpoint_list = tuple(checkpoints)
        direct_candidates: dict[str, set[str]] = defaultdict(set)

        threads = (
            set(self._metadata)
            | set(self._inventory.threads)
            | {checkpoint.rollout_thread_id for checkpoint in checkpoint_list}
        )
        checkpoints_by_thread: dict[str, list[RawTokenCheckpoint]] = defaultdict(list)
        for checkpoint in checkpoint_list:
            checkpoints_by_thread[checkpoint.rollout_thread_id].append(checkpoint)

        for thread_id in threads:
            thread_checkpoints = checkpoints_by_thread.get(thread_id, ())
            if thread_checkpoints:
                for checkpoint in thread_checkpoints:
                    direct = self._attribute_direct(checkpoint)
                    if direct.project_identity is not None:
                        direct_candidates[thread_id].add(direct.project_identity)
            else:
                identity = self._thread_direct_identity(thread_id)
                if identity is not None:
                    direct_candidates[thread_id].add(identity)

        thread_hints = self._build_thread_hints(threads, direct_candidates)

        attributed: list[AttributedCheckpoint] = []
        for checkpoint in checkpoint_list:
            thread_id = checkpoint.rollout_thread_id
            ancestors = tuple(
                thread_hints[ancestor]
                for ancestor in self._lineage.ancestors_of(thread_id)
                if ancestor in thread_hints
            )
            root = self._lineage.root_of(thread_id)
            root_project = (
                thread_hints.get(root)
                if root is not None and root != thread_id
                else None
            )
            descendant_projects = tuple(
                thread_hints[descendant]
                for descendant in self._lineage.descendants_of(thread_id)
                if descendant in thread_hints
            )
            attributed.append(
                AttributedCheckpoint(
                    checkpoint=checkpoint,
                    attribution=attribute_project(
                        manual_project_id=self._manual_project(checkpoint),
                        activity=self._activity_evidence(checkpoint),
                        self_remote=self._self_remote(thread_id),
                        ancestor_projects=ancestors,
                        root_project=root_project,
                        descendant_projects=descendant_projects,
                        local_project_id=self._thread_local_project(thread_id),
                    ),
                )
            )
        return tuple(attributed)

    def _attribute_direct(self, checkpoint: RawTokenCheckpoint) -> ProjectAttribution:
        return attribute_project(
            manual_project_id=self._manual_project(checkpoint),
            activity=self._activity_evidence(checkpoint),
            self_remote=self._self_remote(checkpoint.rollout_thread_id),
            local_project_id=self._thread_local_project(
                checkpoint.rollout_thread_id
            ),
        )

    def _manual_project(self, checkpoint: RawTokenCheckpoint) -> str | None:
        if checkpoint.turn_id is not None:
            project = self._manual_turn_projects.get(checkpoint.turn_id)
            if project is not None:
                return project
        return self._manual_thread_projects.get(checkpoint.rollout_thread_id)

    def _activity_evidence(
        self,
        checkpoint: RawTokenCheckpoint,
    ) -> tuple[ActivityProjectEvidence, ...]:
        evidence: list[ActivityProjectEvidence] = []
        seen_roots: set[str] = set()
        for workdir in checkpoint.activity_workdirs:
            probe = self._probe(workdir)
            if (
                probe is None
                or probe.status != GitProbeStatus.REPOSITORY
                or probe.repository_root is None
            ):
                continue
            repository_root = probe.repository_root
            if repository_root in seen_roots:
                continue
            seen_roots.add(repository_root)
            evidence.append(
                ActivityProjectEvidence(
                    repository_key=repository_root,
                    remote_resolution=probe.remote_resolution,
                    mapped_project_id=self._local_repository_projects.get(
                        repository_root
                    ),
                )
            )
        return tuple(evidence)

    def _self_remote(self, thread_id: str) -> RemoteResolution:
        cached = self._self_remote_cache.get(thread_id)
        if cached is not None:
            return cached

        metadata = self._metadata.get(thread_id)
        inventory_thread = self._inventory.threads.get(thread_id)
        remote_urls = (
            metadata.git_repository_url if metadata is not None else None,
            inventory_thread.git_origin_url if inventory_thread is not None else None,
        )
        for remote_url in remote_urls:
            if remote_url is None:
                continue
            try:
                resolution = resolve_remote(remote_url, (remote_url,))
            except RemoteNormalizationError:
                continue
            if (
                resolution.canonical is not None
                or resolution.kind == RemoteResolutionKind.AMBIGUOUS_REMOTE
            ):
                self._self_remote_cache[thread_id] = resolution
                return resolution

        cwd = (
            metadata.cwd
            if metadata is not None and metadata.cwd is not None
            else inventory_thread.cwd if inventory_thread is not None else None
        )
        if cwd is not None:
            probe = self._probe(cwd)
            if probe is not None and probe.status == GitProbeStatus.REPOSITORY:
                self._self_remote_cache[thread_id] = probe.remote_resolution
                return probe.remote_resolution

        resolution = RemoteResolution(RemoteResolutionKind.UNCLASSIFIED)
        self._self_remote_cache[thread_id] = resolution
        return resolution

    def _thread_local_project(self, thread_id: str) -> str | None:
        metadata = self._metadata.get(thread_id)
        inventory_thread = self._inventory.threads.get(thread_id)
        cwd = (
            metadata.cwd
            if metadata is not None and metadata.cwd is not None
            else inventory_thread.cwd if inventory_thread is not None else None
        )
        if cwd is None:
            return None
        probe = self._probe(cwd)
        if probe is None or probe.repository_root is None:
            return None
        return self._local_repository_projects.get(probe.repository_root)

    def _thread_direct_identity(self, thread_id: str) -> str | None:
        manual = self._manual_thread_projects.get(thread_id)
        if manual is not None:
            return manual
        self_remote = self._self_remote(thread_id)
        if self_remote.canonical is not None:
            return self_remote.canonical
        return self._thread_local_project(thread_id)

    def _build_thread_hints(
        self,
        threads: set[str],
        direct_candidates: Mapping[str, set[str]],
    ) -> dict[str, str]:
        hints = {
            thread_id: next(iter(candidates))
            for thread_id, candidates in direct_candidates.items()
            if len(candidates) == 1
        }
        for thread_id in sorted(threads):
            if thread_id in hints or direct_candidates.get(thread_id):
                continue
            descendant_candidates = {
                hints[descendant]
                for descendant in self._lineage.descendants_of(thread_id)
                if descendant in hints
            }
            if len(descendant_candidates) == 1:
                hints[thread_id] = next(iter(descendant_candidates))
        return hints

    def _probe(self, workdir: str) -> GitProbeResult | None:
        if workdir in self._probe_cache:
            return self._probe_cache[workdir]
        try:
            result = self._git_probe(workdir)
        except (GitProbeError, OSError):
            result = None
        self._probe_cache[workdir] = result
        return result
