"""Read-only local Git repository evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import os
from pathlib import Path
import subprocess

from codex_usage.domain.git_remote import (
    RemoteResolution,
    RemoteResolutionKind,
    resolve_remote,
)


class GitProbeError(RuntimeError):
    """Raised when Git evidence cannot be collected safely."""


class GitProbeStatus(StrEnum):
    """The result of probing one activity working directory."""

    REPOSITORY = "repository"
    NOT_REPOSITORY = "not_repository"
    PATH_MISSING = "path_missing"


@dataclass(frozen=True, slots=True)
class GitProbeResult:
    """Local-only Git evidence for one working directory."""

    status: GitProbeStatus
    repository_root: str | None
    remote_resolution: RemoteResolution


def probe_git_repository(
    workdir: str | Path,
    *,
    git_executable: str = "git",
    timeout_seconds: float = 5.0,
) -> GitProbeResult:
    """Inspect a path with read-only Git commands and resolve its project remote."""

    path = Path(workdir).expanduser()
    if not path.is_dir():
        return GitProbeResult(
            status=GitProbeStatus.PATH_MISSING,
            repository_root=None,
            remote_resolution=RemoteResolution(RemoteResolutionKind.UNCLASSIFIED),
        )
    if timeout_seconds <= 0:
        raise GitProbeError("timeout_seconds must be positive")

    root_result = _run_git(
        git_executable,
        path,
        "rev-parse",
        "--show-toplevel",
        timeout_seconds=timeout_seconds,
    )
    if root_result.returncode != 0:
        return GitProbeResult(
            status=GitProbeStatus.NOT_REPOSITORY,
            repository_root=None,
            remote_resolution=RemoteResolution(RemoteResolutionKind.UNCLASSIFIED),
        )

    repository_root = root_result.stdout.strip()
    if not repository_root:
        raise GitProbeError("Git returned an empty repository root")

    names_result = _run_git(
        git_executable,
        path,
        "remote",
        timeout_seconds=timeout_seconds,
    )
    if names_result.returncode != 0:
        raise GitProbeError("Git failed to list remotes")

    remote_urls: dict[str, str] = {}
    for name in names_result.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        if name.startswith("-") or any(character.isspace() for character in name):
            raise GitProbeError("Git returned an invalid remote name")
        url_result = _run_git(
            git_executable,
            path,
            "remote",
            "get-url",
            name,
            timeout_seconds=timeout_seconds,
        )
        if url_result.returncode != 0 or not url_result.stdout.strip():
            raise GitProbeError("Git failed to read a remote URL")
        remote_urls[name] = url_result.stdout.strip()

    resolution = resolve_remote(remote_urls.get("origin"), list(remote_urls.values()))
    if not remote_urls:
        resolution = RemoteResolution(RemoteResolutionKind.LOCAL_ONLY)

    return GitProbeResult(
        status=GitProbeStatus.REPOSITORY,
        repository_root=repository_root,
        remote_resolution=resolution,
    )


def _run_git(
    executable: str,
    path: Path,
    *arguments: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.run(
            [executable, "-C", str(path), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=creation_flags,
        )
    except FileNotFoundError as error:
        raise GitProbeError("Git executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise GitProbeError("Git command timed out") from error
