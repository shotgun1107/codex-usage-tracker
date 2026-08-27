"""Discovery and complete-line snapshots for local Codex rollout files."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


class RolloutFileError(RuntimeError):
    """A local rollout file cannot be snapshotted safely."""


class RolloutFileBusy(RolloutFileError):
    """The rollout kept changing while a stable snapshot was attempted."""


@dataclass(frozen=True, slots=True)
class PreviousRolloutCursor:
    fingerprint: str
    byte_offset: int


@dataclass(frozen=True, slots=True)
class RolloutFileSnapshot:
    source_id: str
    source_path: str
    fingerprint: str
    byte_offset: int
    last_complete_line_digest: str | None
    lines: tuple[str, ...]


def discover_rollout_files(codex_home: str | Path) -> tuple[Path, ...]:
    """Return active files before archived files with exact paths deduplicated."""

    root = Path(codex_home).expanduser().resolve()
    discovered: list[Path] = []
    seen: set[Path] = set()
    for directory_name in ("sessions", "archived_sessions"):
        directory = root / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.jsonl")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            if not resolved.is_file():
                continue
            seen.add(resolved)
            discovered.append(resolved)
    return tuple(discovered)


def rollout_source_id(path: str | Path) -> str:
    resolved = str(Path(path).expanduser().resolve())
    normalized = resolved.casefold() if os.name == "nt" else resolved
    return "rollout_file_v1_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_changed_rollout(
    path: str | Path,
    previous: PreviousRolloutCursor | None,
    *,
    stability_attempts: int = 3,
) -> RolloutFileSnapshot | None:
    """Read all complete lines only when the filesystem version changed."""

    if stability_attempts < 1:
        raise ValueError("stability_attempts must be positive")
    rollout_path = Path(path).expanduser().resolve()
    if not rollout_path.is_file():
        raise RolloutFileError("rollout path is not a regular file")
    data: bytes | None = None
    fingerprint: str | None = None
    for _ in range(stability_attempts):
        try:
            before = rollout_path.stat()
            before_fingerprint = _stat_fingerprint(before)
            if previous is not None and previous.fingerprint == before_fingerprint:
                return None
            candidate = rollout_path.read_bytes()
            after = rollout_path.stat()
        except OSError as error:
            raise RolloutFileError("rollout file cannot be read") from error
        after_fingerprint = _stat_fingerprint(after)
        if before_fingerprint == after_fingerprint and len(candidate) == after.st_size:
            data = candidate
            fingerprint = after_fingerprint
            break
    if data is None or fingerprint is None:
        raise RolloutFileBusy("rollout changed during every snapshot attempt")

    last_newline = data.rfind(b"\n")
    complete_end = last_newline + 1
    complete = data[:complete_end]
    if complete:
        try:
            text = complete.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RolloutFileError("complete rollout lines are not UTF-8") from error
        lines = tuple(text.splitlines())
        last_digest = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
    else:
        lines = ()
        last_digest = None

    return RolloutFileSnapshot(
        source_id=rollout_source_id(rollout_path),
        source_path=str(rollout_path),
        fingerprint=fingerprint,
        byte_offset=complete_end,
        last_complete_line_digest=last_digest,
        lines=lines,
    )


def _stat_fingerprint(stat: os.stat_result) -> str:
    raw = ":".join(
        str(value)
        for value in (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
        )
    )
    return "stat_v1_" + hashlib.sha256(raw.encode("ascii")).hexdigest()
