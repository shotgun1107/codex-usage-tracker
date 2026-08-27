"""Validate, exchange, and replay the append-only Git ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from codex_usage.application.lock import ApplicationLock
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerReader
from codex_usage.ledger.replay import ReplayResult, replay_ledger_events
from codex_usage.privacy.identifiers import key_id
from codex_usage.storage.read_model import ReadModelState
from codex_usage.storage.sqlite import LocalStateStore
from codex_usage.sync.git import GitLedgerRepository


class SyncError(RuntimeError):
    """Synchronization cannot continue without risking ledger integrity."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class SyncResult:
    branch: str
    changed_files: int
    commit_created: bool
    remote_branch_existed: bool
    rebased: bool
    pushed: bool
    ahead_commits: int
    ledger_event_count: int
    read_model_state: ReadModelState


class SyncService:
    """Synchronize one device-owned ledger directory through Git."""

    def __init__(self, config: AppConfig, shared_key: bytes) -> None:
        if key_id(shared_key) != config.key_id:
            raise SyncError("configured_key_mismatch")
        self.config = config

    def sync(self) -> SyncResult:
        with ApplicationLock(self.config.state_db):
            return self._locked_sync()

    def _locked_sync(self) -> SyncResult:
        store = LocalStateStore(self.config.state_db)
        run_id = store.begin_sync_run()
        try:
            result = self._sync(store)
        except Exception as error:
            store.finish_sync_run(run_id, "failed", _detail_code(error))
            raise
        store.finish_sync_run(run_id, "succeeded")
        return result

    def _sync(self, store: LocalStateStore) -> SyncResult:
        repository = GitLedgerRepository(self.config.ledger_root)
        branch = repository.validate()
        changes = repository.working_changes()
        repository.validate_own_changes(changes, self.config.device_id)
        _validated_replay(self.config)

        commit_created = False
        if changes:
            repository.stage_device(self.config.device_id)
            repository.validate_own_changes(
                repository.working_changes(),
                self.config.device_id,
            )
            if not repository.has_staged_changes():
                raise SyncError("device_changes_not_staged")
            repository.commit(_commit_message(self.config.device_id))
            commit_created = True

        repository.fetch()
        remote_exists = repository.remote_branch_exists(branch)
        if remote_exists:
            repository.validate_local_commits(branch, self.config.device_id)
            repository.rebase(branch)
        elif repository.has_head():
            raise SyncError("remote_branch_missing")

        if repository.working_changes():
            raise SyncError("worktree_not_clean_after_rebase")
        ahead = repository.ahead_of_remote(branch) if remote_exists else 0
        if ahead:
            repository.validate_outgoing_changes(branch, self.config.device_id)
        replay = _validated_replay(self.config)
        read_model_state = store.rebuild_read_model(replay)

        pushed = ahead > 0
        if pushed:
            repository.push(branch)

        return SyncResult(
            branch=branch,
            changed_files=len(changes),
            commit_created=commit_created,
            remote_branch_existed=remote_exists,
            rebased=remote_exists,
            pushed=pushed,
            ahead_commits=ahead,
            ledger_event_count=replay.input_event_count,
            read_model_state=read_model_state,
        )


def _validated_replay(config: AppConfig) -> ReplayResult:
    read = LedgerReader(config.ledger_root).read_all()
    if read.issues:
        raise SyncError("ledger_contains_partial_line")
    return replay_ledger_events(read.events, expected_key_id=config.key_id)


def _commit_message(device_id: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"codex-usage: sync {device_id[:8]} {timestamp}"


def _detail_code(error: Exception) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(error).__name__
