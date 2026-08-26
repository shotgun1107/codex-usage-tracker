"""Deterministic project attribution for one Codex token event."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from codex_usage.domain.git_remote import RemoteResolution, RemoteResolutionKind


class ProjectResolutionKind(StrEnum):
    """Why a usage event was assigned, withheld, or left unclassified."""

    MANUAL = "manual"
    ACTIVITY_GIT = "activity_git"
    SELF_ORIGIN = "self_origin"
    UNIQUE_REMOTE = "unique_remote"
    ANCESTOR = "ancestor"
    ROOT = "root"
    DESCENDANT_CONSENSUS = "descendant_consensus"
    LOCAL_MAPPING = "local_mapping"
    AMBIGUOUS_REMOTE = "ambiguous_remote"
    AMBIGUOUS_MULTI_REPO = "ambiguous_multi_repo"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class ActivityProjectEvidence:
    """Git evidence for one distinct repository touched during a turn."""

    repository_key: str
    remote_resolution: RemoteResolution
    mapped_project_id: str | None = None

    def __post_init__(self) -> None:
        if not self.repository_key:
            raise ValueError("repository_key must not be empty")
        if self.mapped_project_id == "":
            raise ValueError("mapped_project_id must not be empty")


@dataclass(frozen=True, slots=True)
class ProjectAttribution:
    """The local project identity and the evidence used to select it."""

    project_identity: str | None
    resolution: ProjectResolutionKind
    activity_repository_count: int
    candidates: tuple[str, ...] = ()


def attribute_project(
    *,
    manual_project_id: str | None = None,
    activity: Iterable[ActivityProjectEvidence] = (),
    self_remote: RemoteResolution | None = None,
    ancestor_projects: Iterable[str] = (),
    root_project: str | None = None,
    descendant_projects: Iterable[str] = (),
    local_project_id: str | None = None,
) -> ProjectAttribution:
    """Apply the approved turn-level project attribution priority."""

    manual_project_id = _optional_identity(manual_project_id)
    root_project = _optional_identity(root_project)
    local_project_id = _optional_identity(local_project_id)

    repository_evidence = _coalesce_activity(activity)
    repository_count = len(repository_evidence)

    if manual_project_id is not None:
        return ProjectAttribution(
            manual_project_id,
            ProjectResolutionKind.MANUAL,
            repository_count,
            (manual_project_id,),
        )

    activity_result = _attribute_activity(repository_evidence)
    if activity_result is not None:
        return activity_result

    if self_remote is not None:
        if self_remote.canonical is not None:
            resolution = (
                ProjectResolutionKind.SELF_ORIGIN
                if self_remote.kind == RemoteResolutionKind.ORIGIN
                else ProjectResolutionKind.UNIQUE_REMOTE
            )
            return ProjectAttribution(
                self_remote.canonical,
                resolution,
                repository_count,
                (self_remote.canonical,),
            )
        if self_remote.kind == RemoteResolutionKind.AMBIGUOUS_REMOTE:
            return ProjectAttribution(
                None,
                ProjectResolutionKind.AMBIGUOUS_REMOTE,
                repository_count,
                tuple(sorted(set(self_remote.candidates))),
            )

    ancestors = _ordered_unique(ancestor_projects)
    if ancestors:
        return ProjectAttribution(
            ancestors[0],
            ProjectResolutionKind.ANCESTOR,
            repository_count,
            ancestors,
        )

    if root_project is not None:
        return ProjectAttribution(
            root_project,
            ProjectResolutionKind.ROOT,
            repository_count,
            (root_project,),
        )

    descendants = tuple(sorted(set(_validated_identities(descendant_projects))))
    if len(descendants) == 1:
        return ProjectAttribution(
            descendants[0],
            ProjectResolutionKind.DESCENDANT_CONSENSUS,
            repository_count,
            descendants,
        )

    if local_project_id is not None:
        return ProjectAttribution(
            local_project_id,
            ProjectResolutionKind.LOCAL_MAPPING,
            repository_count,
            (local_project_id,),
        )

    return ProjectAttribution(
        None,
        ProjectResolutionKind.UNCLASSIFIED,
        repository_count,
        descendants,
    )


def _coalesce_activity(
    activity: Iterable[ActivityProjectEvidence],
) -> dict[str, tuple[ActivityProjectEvidence, ...]]:
    grouped: dict[str, list[ActivityProjectEvidence]] = {}
    for evidence in activity:
        grouped.setdefault(evidence.repository_key, []).append(evidence)
    return {key: tuple(values) for key, values in grouped.items()}


def _attribute_activity(
    repositories: dict[str, tuple[ActivityProjectEvidence, ...]],
) -> ProjectAttribution | None:
    if not repositories:
        return None

    resolved: list[tuple[str, bool]] = []
    unresolved = False
    ambiguous_candidates: set[str] = set()

    for evidence_items in repositories.values():
        mapped = {
            item.mapped_project_id
            for item in evidence_items
            if item.mapped_project_id is not None
        }
        remote_candidates = {
            item.remote_resolution.canonical
            for item in evidence_items
            if item.remote_resolution.canonical is not None
        }
        for item in evidence_items:
            if item.remote_resolution.kind == RemoteResolutionKind.AMBIGUOUS_REMOTE:
                ambiguous_candidates.update(item.remote_resolution.candidates)

        identities = mapped or remote_candidates
        if len(identities) == 1 and not ambiguous_candidates:
            resolved.append((next(iter(identities)), not bool(mapped)))
        else:
            unresolved = True
            ambiguous_candidates.update(identity for identity in identities if identity)

    all_candidates = {identity for identity, _ in resolved} | ambiguous_candidates
    if unresolved:
        kind = (
            ProjectResolutionKind.AMBIGUOUS_REMOTE
            if len(repositories) == 1 and ambiguous_candidates
            else ProjectResolutionKind.AMBIGUOUS_MULTI_REPO
        )
        if len(repositories) == 1 and not ambiguous_candidates:
            return None
        return ProjectAttribution(
            None,
            kind,
            len(repositories),
            tuple(sorted(all_candidates)),
        )

    identities = {identity for identity, _ in resolved}
    if len(identities) > 1:
        return ProjectAttribution(
            None,
            ProjectResolutionKind.AMBIGUOUS_MULTI_REPO,
            len(repositories),
            tuple(sorted(identities)),
        )
    if len(identities) == 1:
        identity = next(iter(identities))
        used_remote = any(remote for _, remote in resolved)
        return ProjectAttribution(
            identity,
            (
                ProjectResolutionKind.ACTIVITY_GIT
                if used_remote
                else ProjectResolutionKind.LOCAL_MAPPING
            ),
            len(repositories),
            (identity,),
        )
    return None


def _optional_identity(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("project identity must not be empty")
    return value


def _validated_identities(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("project identity must not be empty")
        result.append(value)
    return tuple(result)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_validated_identities(values)))
