"""Fail-closed Git operations for one device's append-only ledger area."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import subprocess


class GitSyncError(RuntimeError):
    """A Git sync step failed without automatic conflict resolution."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkingChange:
    index_status: str
    worktree_status: str
    relative_path: str


class GitLedgerRepository:
    """Run non-interactive, bounded Git commands without exposing output."""

    def __init__(
        self,
        root: str | Path,
        *,
        git_executable: str = "git",
        timeout_seconds: float = 120.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.root = Path(root).expanduser().resolve()
        self.git_executable = git_executable
        self.timeout_seconds = timeout_seconds

    def validate(self) -> str:
        if not self.root.is_dir():
            raise GitSyncError("ledger_root_missing")
        result = self._run("rev-parse", "--is-inside-work-tree")
        if result.stdout.strip() != "true":
            raise GitSyncError("ledger_not_git_repository")
        top_level = Path(
            self._run("rev-parse", "--show-toplevel").stdout.strip()
        ).resolve()
        if top_level != self.root:
            raise GitSyncError("ledger_root_not_repository_root")
        self._run("remote", "get-url", "origin")
        for ref in ("REBASE_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
            state = self._run("rev-parse", "-q", "--verify", ref, check=False)
            if state.returncode == 0:
                raise GitSyncError("git_operation_in_progress")
        branch = self._run("branch", "--show-current").stdout.strip()
        if not branch:
            raise GitSyncError("detached_head")
        return branch

    def working_changes(self) -> tuple[WorkingChange, ...]:
        result = self._run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        fields = result.stdout.split("\x00")
        changes: list[WorkingChange] = []
        index = 0
        while index < len(fields):
            field = fields[index]
            index += 1
            if not field:
                continue
            if len(field) < 4 or field[2] != " ":
                raise GitSyncError("git_status_unrecognized")
            index_status, worktree_status = field[0], field[1]
            if index_status in {"R", "C"} or worktree_status in {"R", "C"}:
                raise GitSyncError("ledger_rename_forbidden")
            changes.append(
                WorkingChange(
                    index_status=index_status,
                    worktree_status=worktree_status,
                    relative_path=_safe_git_path(field[3:]),
                )
            )
        return tuple(changes)

    def validate_own_changes(
        self,
        changes: tuple[WorkingChange, ...],
        device_id: str,
    ) -> None:
        prefix = f"devices/{device_id}/"
        for change in changes:
            if not change.relative_path.startswith(prefix):
                raise GitSyncError("change_outside_device_directory")
            if "D" in {change.index_status, change.worktree_status}:
                raise GitSyncError("ledger_deletion_forbidden")
            if not change.relative_path.endswith(".jsonl"):
                raise GitSyncError("non_jsonl_device_change")
            self._validate_append_only(
                change.relative_path,
                is_new=change.index_status in {"?", "A"},
            )

    def stage_device(self, device_id: str) -> None:
        self._run("add", "--", f"devices/{device_id}")

    def has_staged_changes(self) -> bool:
        result = self._run("diff", "--cached", "--quiet", check=False)
        if result.returncode not in {0, 1}:
            raise GitSyncError("staged_diff_failed")
        return result.returncode == 1

    def commit(self, message: str) -> None:
        if not message or "\n" in message or "\r" in message:
            raise ValueError("commit message must be one non-empty line")
        self._run("commit", "--no-verify", "-m", message)

    def fetch(self) -> None:
        self._run("fetch", "--prune", "origin")

    def remote_branch_exists(self, branch: str) -> bool:
        result = self._run(
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/remotes/origin/{branch}",
            check=False,
        )
        if result.returncode not in {0, 1}:
            raise GitSyncError("remote_branch_check_failed")
        return result.returncode == 0

    def rebase(self, branch: str) -> None:
        self._run("rebase", f"origin/{branch}")

    def has_head(self) -> bool:
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        return result.returncode == 0

    def ahead_of_remote(self, branch: str) -> int:
        result = self._run(
            "rev-list",
            "--count",
            f"origin/{branch}..HEAD",
        )
        try:
            return int(result.stdout.strip())
        except ValueError as error:
            raise GitSyncError("ahead_count_invalid") from error

    def validate_outgoing_changes(self, branch: str, device_id: str) -> None:
        """Reject already-committed outgoing data outside this device boundary."""

        self._validate_committed_changes(
            f"origin/{branch}",
            "HEAD",
            device_id,
        )

    def validate_local_commits(self, branch: str, device_id: str) -> None:
        """Validate the local side of a divergence before attempting rebase."""

        result = self._run(
            "merge-base",
            f"origin/{branch}",
            "HEAD",
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise GitSyncError("git_histories_unrelated")
        self._validate_committed_changes(
            result.stdout.strip(),
            "HEAD",
            device_id,
        )

    def _validate_committed_changes(
        self,
        base_ref: str,
        target_ref: str,
        device_id: str,
    ) -> None:
        result = self._run(
            "diff",
            "--name-status",
            "-z",
            f"{base_ref}..{target_ref}",
        )
        fields = result.stdout.split("\x00")
        prefix = f"devices/{device_id}/"
        index = 0
        while index < len(fields):
            status = fields[index]
            index += 1
            if not status:
                continue
            if index >= len(fields) or not fields[index]:
                raise GitSyncError("outgoing_diff_unrecognized")
            relative_path = _safe_git_path(fields[index])
            index += 1
            if status.startswith(("R", "C")):
                raise GitSyncError("outgoing_rename_forbidden")
            if status not in {"A", "M"}:
                raise GitSyncError("outgoing_change_forbidden")
            if not relative_path.startswith(prefix):
                raise GitSyncError("outgoing_change_outside_device_directory")
            if not relative_path.endswith(".jsonl"):
                raise GitSyncError("outgoing_non_jsonl_device_change")
            if status == "M":
                self._validate_ref_append_only(
                    base_ref,
                    target_ref,
                    relative_path,
                )

    def push(self, branch: str) -> None:
        self._run(
            "push",
            "--no-verify",
            "--set-upstream",
            "origin",
            f"HEAD:{branch}",
        )

    def _validate_append_only(self, relative_path: str, *, is_new: bool) -> None:
        if is_new or not self.has_head():
            return
        exists = self._run(
            "cat-file",
            "-e",
            f"HEAD:{relative_path}",
            check=False,
        )
        if exists.returncode != 0:
            raise GitSyncError("head_blob_check_failed")
        original = self._run_bytes("show", f"HEAD:{relative_path}")
        target = self.root.joinpath(*PurePosixPath(relative_path).parts)
        try:
            current = target.read_bytes()
        except OSError as error:
            raise GitSyncError("ledger_file_unreadable") from error
        if not current.startswith(original):
            raise GitSyncError("ledger_history_rewrite_forbidden")

    def _validate_ref_append_only(
        self,
        base_ref: str,
        target_ref: str,
        relative_path: str,
    ) -> None:
        original = self._run_bytes("show", f"{base_ref}:{relative_path}")
        current = self._run_bytes("show", f"{target_ref}:{relative_path}")
        if not current.startswith(original):
            raise GitSyncError("outgoing_ledger_history_rewrite_forbidden")

    def _run(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.git_executable, "-C", str(self.root), *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                creationflags=_creation_flags(),
                env=_git_environment(),
            )
        except FileNotFoundError as error:
            raise GitSyncError("git_not_found") from error
        except subprocess.TimeoutExpired as error:
            raise GitSyncError("git_command_timed_out") from error
        if check and result.returncode != 0:
            command = arguments[0] if arguments else "unknown"
            raise GitSyncError(f"git_{command}_failed")
        return result

    def _run_bytes(self, *arguments: str) -> bytes:
        try:
            result = subprocess.run(
                [self.git_executable, "-C", str(self.root), *arguments],
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout_seconds,
                creationflags=_creation_flags(),
                env=_git_environment(),
            )
        except FileNotFoundError as error:
            raise GitSyncError("git_not_found") from error
        except subprocess.TimeoutExpired as error:
            raise GitSyncError("git_command_timed_out") from error
        if result.returncode != 0:
            raise GitSyncError("git_binary_read_failed")
        return result.stdout


def _safe_git_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
    ):
        raise GitSyncError("git_status_path_unsafe")
    return path.as_posix()


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return environment
