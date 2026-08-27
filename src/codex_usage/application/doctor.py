"""Read-only diagnostics for a configured Codex Usage Tracker device."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import shutil
import sqlite3
import subprocess

from codex_usage.application.collect import find_codex_state_database
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerIoError, LedgerReader
from codex_usage.ledger.schema_validation import LedgerSchemaError
from codex_usage.privacy.guard import PrivacyViolation
from codex_usage.privacy.identifiers import key_id
from codex_usage.sources.rollout_files import discover_rollout_files


class CheckStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class DoctorResult:
    checks: tuple[DoctorCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status is CheckStatus.ERROR for check in self.checks)


def run_doctor(config: AppConfig, shared_key: bytes) -> DoctorResult:
    checks: list[DoctorCheck] = []
    checks.append(
        DoctorCheck(
            "shared-key",
            CheckStatus.OK if key_id(shared_key) == config.key_id else CheckStatus.ERROR,
            "configured key matches" if key_id(shared_key) == config.key_id else "key mismatch",
        )
    )

    codex_home = Path(config.codex_home)
    if codex_home.is_dir():
        rollout_count = len(discover_rollout_files(codex_home))
        checks.append(
            DoctorCheck(
                "codex-rollouts",
                CheckStatus.OK if rollout_count else CheckStatus.WARNING,
                f"{rollout_count} rollout files discovered",
            )
        )
        checks.append(
            DoctorCheck(
                "codex-state",
                (
                    CheckStatus.OK
                    if find_codex_state_database(codex_home) is not None
                    else CheckStatus.WARNING
                ),
                (
                    "Codex SQLite state found"
                    if find_codex_state_database(codex_home) is not None
                    else "Codex SQLite state missing; JSONL-only fallback will be used"
                ),
            )
        )
    else:
        checks.append(
            DoctorCheck("codex-rollouts", CheckStatus.ERROR, "Codex home is missing")
        )

    state_path = Path(config.state_db)
    if state_path.is_file():
        checks.append(_check_local_sqlite(state_path))
    else:
        checks.append(
            DoctorCheck(
                "local-state",
                CheckStatus.WARNING,
                "local state database has not been created",
            )
        )

    ledger_root = Path(config.ledger_root)
    if ledger_root.is_dir():
        try:
            read = LedgerReader(ledger_root).read_all()
            key_ids = {
                event.get("key_id")
                for event in read.events
                if isinstance(event.get("key_id"), str)
            }
            if key_ids - {config.key_id}:
                raise LedgerIoError("ledger key_id mismatch")
            checks.append(
                DoctorCheck(
                    "ledger",
                    CheckStatus.OK,
                    f"{len(read.events)} validated events; {len(read.issues)} partial lines",
                )
            )
        except (LedgerIoError, LedgerSchemaError, PrivacyViolation):
            checks.append(
                DoctorCheck(
                    "ledger",
                    CheckStatus.ERROR,
                    "ledger validation failed",
                )
            )
        checks.append(_check_git_repository(ledger_root))
    else:
        checks.append(
            DoctorCheck("ledger", CheckStatus.ERROR, "ledger root is missing")
        )

    return DoctorResult(tuple(checks))


def _check_local_sqlite(path: Path) -> DoctorCheck:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
        status = CheckStatus.OK if result == "ok" else CheckStatus.ERROR
        message = "SQLite integrity check passed" if result == "ok" else "SQLite is invalid"
        return DoctorCheck("local-state", status, message)
    except sqlite3.Error:
        return DoctorCheck(
            "local-state",
            CheckStatus.ERROR,
            "local state database cannot be read",
        )
    finally:
        if connection is not None:
            connection.close()


def _check_git_repository(path: Path) -> DoctorCheck:
    executable = shutil.which("git")
    if executable is None:
        return DoctorCheck("ledger-git", CheckStatus.ERROR, "Git is not installed")
    creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        result = subprocess.run(
            [executable, "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return DoctorCheck("ledger-git", CheckStatus.ERROR, "Git check failed")
    if result.returncode == 0 and result.stdout.strip() == "true":
        return DoctorCheck("ledger-git", CheckStatus.OK, "ledger is a Git repository")
    return DoctorCheck(
        "ledger-git",
        CheckStatus.WARNING,
        "ledger is not a Git repository yet",
    )
