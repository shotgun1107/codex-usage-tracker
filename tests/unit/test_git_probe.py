from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from codex_usage.domain.git_remote import RemoteResolutionKind
from codex_usage.sources.git import (
    GitProbeError,
    GitProbeStatus,
    probe_git_repository,
)


def run_git(path: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class GitProbeTests(unittest.TestCase):
    def test_missing_path_and_plain_directory_are_not_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            missing = probe_git_repository(root / "missing")
            plain = probe_git_repository(root)

            self.assertEqual(missing.status, GitProbeStatus.PATH_MISSING)
            self.assertEqual(plain.status, GitProbeStatus.NOT_REPOSITORY)
            self.assertEqual(
                plain.remote_resolution.kind,
                RemoteResolutionKind.UNCLASSIFIED,
            )

    def test_repository_without_remote_is_local_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "한글 저장소"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")

            result = probe_git_repository(repository)

            self.assertEqual(result.status, GitProbeStatus.REPOSITORY)
            self.assertEqual(
                result.remote_resolution.kind,
                RemoteResolutionKind.LOCAL_ONLY,
            )
            self.assertEqual(Path(result.repository_root or "").resolve(), repository.resolve())

    def test_origin_is_preferred_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")
            run_git(
                repository,
                "remote",
                "add",
                "origin",
                "git@github.com:Example/Project.git",
            )
            run_git(
                repository,
                "remote",
                "add",
                "upstream",
                "https://github.com/Other/Project.git",
            )

            result = probe_git_repository(repository)

            self.assertEqual(
                result.remote_resolution.kind,
                RemoteResolutionKind.ORIGIN,
            )
            self.assertEqual(
                result.remote_resolution.canonical,
                "github.com/example/project",
            )

    def test_one_non_origin_remote_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")
            run_git(
                repository,
                "remote",
                "add",
                "upstream",
                "https://git.example.com/Team/Project.git",
            )

            result = probe_git_repository(repository)

            self.assertEqual(
                result.remote_resolution.kind,
                RemoteResolutionKind.UNIQUE_REMOTE,
            )
            self.assertEqual(
                result.remote_resolution.canonical,
                "git.example.com/Team/Project",
            )

    def test_multiple_non_origin_remotes_are_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            run_git(repository, "init", "-b", "main")
            run_git(repository, "remote", "add", "one", "https://one.example/repo")
            run_git(repository, "remote", "add", "two", "https://two.example/repo")

            result = probe_git_repository(repository)

            self.assertEqual(
                result.remote_resolution.kind,
                RemoteResolutionKind.AMBIGUOUS_REMOTE,
            )
            self.assertEqual(len(result.remote_resolution.candidates), 2)

    def test_invalid_timeout_and_missing_executable_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with self.assertRaises(GitProbeError):
                probe_git_repository(path, timeout_seconds=0)
            with self.assertRaises(GitProbeError):
                probe_git_repository(path, git_executable="missing-git-executable")


if __name__ == "__main__":
    unittest.main()
