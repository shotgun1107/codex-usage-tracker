"""Thread lineage reconstructed from Codex JSONL and SQLite evidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Iterable, Mapping


class ParentEvidenceSource(StrEnum):
    """The source that asserted one direct parent relationship."""

    SQLITE_SPAWN = "sqlite_spawn"
    SESSION_SOURCE = "session_source"
    FORK = "fork"


_SOURCE_PRIORITY = {
    ParentEvidenceSource.SQLITE_SPAWN: 30,
    ParentEvidenceSource.SESSION_SOURCE: 20,
    ParentEvidenceSource.FORK: 10,
}


@dataclass(frozen=True, slots=True)
class ParentCandidate:
    """One possible direct parent for a thread."""

    child_thread_id: str
    parent_thread_id: str
    source: ParentEvidenceSource

    def __post_init__(self) -> None:
        if not self.child_thread_id or not self.parent_thread_id:
            raise ValueError("lineage thread IDs must not be empty")


@dataclass(frozen=True, slots=True)
class LineageIssue:
    """A non-fatal inconsistency that prevented unsafe inheritance."""

    thread_id: str
    code: str


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """An immutable, acyclic view of direct and root thread relationships."""

    parent_by_child: Mapping[str, str]
    declared_root_by_thread: Mapping[str, str]
    issues: tuple[LineageIssue, ...]

    def parent_of(self, thread_id: str) -> str | None:
        return self.parent_by_child.get(thread_id)

    def ancestors_of(self, thread_id: str) -> tuple[str, ...]:
        """Return parents nearest-first; cycles have already been removed."""

        ancestors: list[str] = []
        current = thread_id
        while current in self.parent_by_child:
            current = self.parent_by_child[current]
            ancestors.append(current)
        return tuple(ancestors)

    def root_of(self, thread_id: str) -> str | None:
        """Return declared root when available, otherwise the parent-chain root."""

        declared = self.declared_root_by_thread.get(thread_id)
        if declared is not None:
            return declared
        ancestors = self.ancestors_of(thread_id)
        return ancestors[-1] if ancestors else None

    def descendants_of(self, thread_id: str) -> tuple[str, ...]:
        children: dict[str, list[str]] = defaultdict(list)
        for child, parent in self.parent_by_child.items():
            children[parent].append(child)

        descendants: list[str] = []
        pending = list(sorted(children.get(thread_id, ()), reverse=True))
        while pending:
            child = pending.pop()
            descendants.append(child)
            pending.extend(sorted(children.get(child, ()), reverse=True))
        return tuple(descendants)


def build_lineage(
    thread_ids: Iterable[str],
    parent_candidates: Iterable[ParentCandidate],
    declared_roots: Mapping[str, str] | None = None,
) -> LineageGraph:
    """Resolve direct parents by source priority and remove unsafe cycles."""

    known_threads = set(thread_ids)
    if any(not thread_id for thread_id in known_threads):
        raise ValueError("thread IDs must not be empty")

    issues: list[LineageIssue] = []
    grouped: dict[str, list[ParentCandidate]] = defaultdict(list)
    for candidate in parent_candidates:
        known_threads.add(candidate.child_thread_id)
        known_threads.add(candidate.parent_thread_id)
        grouped[candidate.child_thread_id].append(candidate)

    parent_by_child: dict[str, str] = {}
    for child, candidates in grouped.items():
        if any(candidate.parent_thread_id == child for candidate in candidates):
            issues.append(LineageIssue(child, "self_parent_ignored"))
            candidates = [
                candidate
                for candidate in candidates
                if candidate.parent_thread_id != child
            ]
        if not candidates:
            continue

        best_priority = max(_SOURCE_PRIORITY[candidate.source] for candidate in candidates)
        best_parents = {
            candidate.parent_thread_id
            for candidate in candidates
            if _SOURCE_PRIORITY[candidate.source] == best_priority
        }
        all_parents = {candidate.parent_thread_id for candidate in candidates}
        if len(best_parents) != 1:
            issues.append(LineageIssue(child, "conflicting_parent_ignored"))
            continue
        selected = next(iter(best_parents))
        parent_by_child[child] = selected
        if len(all_parents) > 1:
            issues.append(LineageIssue(child, "lower_priority_parent_ignored"))

    cycle_nodes = _find_cycle_nodes(parent_by_child)
    for thread_id in sorted(cycle_nodes):
        parent_by_child.pop(thread_id, None)
        issues.append(LineageIssue(thread_id, "lineage_cycle_ignored"))

    roots: dict[str, str] = {}
    for thread_id, root_id in (declared_roots or {}).items():
        if not thread_id or not root_id:
            raise ValueError("declared root IDs must not be empty")
        roots[thread_id] = root_id
        known_threads.update((thread_id, root_id))

    return LineageGraph(
        parent_by_child=MappingProxyType(parent_by_child),
        declared_root_by_thread=MappingProxyType(roots),
        issues=tuple(issues),
    )


def _find_cycle_nodes(parent_by_child: Mapping[str, str]) -> set[str]:
    cycle_nodes: set[str] = set()
    finished: set[str] = set()

    for start in parent_by_child:
        if start in finished:
            continue
        path: list[str] = []
        position: dict[str, int] = {}
        current = start
        while current in parent_by_child and current not in finished:
            if current in position:
                cycle_nodes.update(path[position[current] :])
                break
            position[current] = len(path)
            path.append(current)
            current = parent_by_child[current]
        finished.update(path)
    return cycle_nodes
