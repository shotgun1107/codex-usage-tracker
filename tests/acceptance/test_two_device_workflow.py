from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from codex_usage.application.doctor import CheckStatus, run_doctor
from codex_usage.cli import main
from codex_usage.config import AppConfig, save_config
from codex_usage.domain.git_remote import normalize_remote
from codex_usage.privacy.identifiers import generate_shared_key, key_id, project_id
from codex_usage.reports.query import ReportQuery, build_usage_report
from codex_usage.secret_store import MemorySecretStore


DEVICE_HOME = "00000000-0000-4000-8000-000000000091"
DEVICE_WORK = "00000000-0000-4000-8000-000000000092"
DEVICE_REBUILD = "00000000-0000-4000-8000-000000000093"
PROJECT_REMOTE = "https://github.com/example/shared-project.git"
RAW_UNRESOLVED_THREAD = "019f0000-0000-7000-8000-000000000099"


@unittest.skipUnless(shutil.which("git"), "Git is required")
class TwoDeviceWorkflowAcceptanceTests(unittest.TestCase):
    def test_collect_sync_link_report_and_clean_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = _create_remote(root)
            shared_key = generate_shared_key()
            secrets = MemorySecretStore()

            home = _create_device(
                root,
                "home",
                remote,
                DEVICE_HOME,
                shared_key,
                (
                    _RolloutSpec(
                        "home-project-thread",
                        "home-project-turn",
                        100,
                        PROJECT_REMOTE,
                        "C:/home/folder-with-different-name",
                    ),
                    _RolloutSpec(
                        RAW_UNRESOLVED_THREAD,
                        "home-unresolved-turn",
                        50,
                        None,
                        "C:/orchestration/outside-project",
                    ),
                ),
            )
            work = _create_device(
                root,
                "work",
                remote,
                DEVICE_WORK,
                shared_key,
                (
                    _RolloutSpec(
                        "work-project-thread",
                        "work-project-turn",
                        200,
                        PROJECT_REMOTE,
                        "D:/company/completely-different-folder-name",
                    ),
                ),
            )
            secrets.put(home.config.credential_target, shared_key)

            self.assertEqual(_cli(home, secrets, "collect"), 0)
            self.assertEqual(_cli(home, secrets, "sync"), 0)
            self.assertEqual(_cli(work, secrets, "collect"), 0)
            self.assertEqual(_cli(work, secrets, "sync"), 0)
            self.assertEqual(_cli(home, secrets, "sync"), 0)

            normalized = normalize_remote(PROJECT_REMOTE)
            assert normalized is not None
            shared_project_id = project_id(shared_key, normalized)
            unresolved_output = StringIO()
            self.assertEqual(
                _cli(
                    home,
                    secrets,
                    "project",
                    "unresolved",
                    stdout=unresolved_output,
                ),
                0,
            )
            self.assertIn(RAW_UNRESOLVED_THREAD, unresolved_output.getvalue())
            self.assertEqual(
                _cli(
                    home,
                    secrets,
                    "project",
                    "link",
                    "--thread",
                    RAW_UNRESOLVED_THREAD,
                    "--project",
                    shared_project_id,
                ),
                0,
            )
            self.assertEqual(_cli(home, secrets, "sync"), 0)
            self.assertEqual(_cli(work, secrets, "sync"), 0)

            for device in (home, work):
                report = build_usage_report(
                    device.config.state_db,
                    ReportQuery(group_by=("project",)),
                )
                self.assertEqual(report.total.total_tokens.value, 350)
                self.assertEqual(len(report.rows), 1)
                self.assertEqual(report.rows[0].tokens.total_tokens.value, 350)
                doctor = run_doctor(device.config, shared_key)
                self.assertFalse(doctor.has_errors)
                self.assertTrue(
                    all(check.status is CheckStatus.OK for check in doctor.checks),
                    tuple((check.name, check.status, check.message) for check in doctor.checks),
                )

            rebuilt = _create_device(
                root,
                "rebuild",
                remote,
                DEVICE_REBUILD,
                shared_key,
                (),
            )
            self.assertEqual(_cli(rebuilt, secrets, "sync"), 0)
            rebuilt_report = build_usage_report(
                rebuilt.config.state_db,
                ReportQuery(group_by=("project", "device")),
            )
            self.assertEqual(rebuilt_report.total.total_tokens.value, 350)
            self.assertEqual(
                sorted(row.tokens.total_tokens.value for row in rebuilt_report.rows),
                [150, 200],
            )


class _RolloutSpec:
    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        total_tokens: int,
        remote: str | None,
        cwd: str,
    ) -> None:
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.total_tokens = total_tokens
        self.remote = remote
        self.cwd = cwd


class _Device:
    def __init__(self, config: AppConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path


def _create_device(
    root: Path,
    name: str,
    remote: Path,
    device_id: str,
    shared_key: bytes,
    rollouts: tuple[_RolloutSpec, ...],
) -> _Device:
    device_root = root / name
    ledger = device_root / "ledger"
    device_root.mkdir()
    _git(None, "clone", str(remote), str(ledger))
    _configure_identity(ledger)
    codex_home = device_root / "codex-home"
    codex_home.mkdir()
    rollout_paths: list[tuple[_RolloutSpec, Path]] = []
    for index, spec in enumerate(rollouts):
        path = codex_home / "sessions" / "2026" / f"rollout-{index}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_rollout(path, spec, index)
        rollout_paths.append((spec, path))
    _write_codex_state(codex_home / "state_5.sqlite", rollout_paths)

    current_key_id = key_id(shared_key)
    config = AppConfig(
        schema_version=1,
        device_id=device_id,
        codex_home=str(codex_home.resolve()),
        state_db=str((device_root / "state.sqlite").resolve()),
        ledger_root=str(ledger.resolve()),
        credential_target=f"CodexUsageTracker/{current_key_id}",
        key_id=current_key_id,
    )
    config_path = device_root / "config.json"
    save_config(config, config_path)
    return _Device(config, config_path)


def _write_rollout(path: Path, spec: _RolloutSpec, ordinal: int) -> None:
    timestamp = f"2026-08-2{ordinal + 6}T0{ordinal}:00:00Z"
    git = (
        {
            "repository_url": spec.remote,
            "branch": "main",
            "commit_hash": "a" * 40,
        }
        if spec.remote is not None
        else None
    )
    metadata = {
        "id": spec.thread_id,
        "session_id": spec.thread_id,
        "source": "exec",
        "cli_version": "0.150.0-acceptance",
        "cwd": spec.cwd,
    }
    if git is not None:
        metadata["git"] = git
    counts = {
        "input_tokens": spec.total_tokens - 10,
        "cached_input_tokens": min(5, spec.total_tokens - 10),
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
        "total_tokens": spec.total_tokens,
    }
    records = (
        {"timestamp": timestamp, "type": "session_meta", "payload": metadata},
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": spec.turn_id},
        },
        {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {
                "turn_id": spec.turn_id,
                "model": "gpt-acceptance",
                "effort": "medium",
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": counts,
                    "last_token_usage": counts,
                },
            },
        },
    )
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_codex_state(
    path: Path,
    rollouts: list[tuple[_RolloutSpec, Path]],
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                rollout_path TEXT,
                cwd TEXT,
                git_origin_url TEXT,
                cli_version TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE thread_spawn_edges (
                parent_thread_id TEXT,
                child_thread_id TEXT,
                status TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO threads (
                id, rollout_path, cwd, git_origin_url, cli_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                (
                    spec.thread_id,
                    str(path_value),
                    spec.cwd,
                    spec.remote,
                    "0.150.0-acceptance",
                )
                for spec, path_value in rollouts
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _create_remote(root: Path) -> Path:
    remote = root / "remote.git"
    _git(None, "init", "--bare", str(remote))
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init")
    _configure_identity(seed)
    _git(seed, "branch", "-M", "main")
    (seed / "README.md").write_text("private ledger\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "Initialize ledger")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def _cli(
    device: _Device,
    secrets: MemorySecretStore,
    *arguments: str,
    stdout: StringIO | None = None,
) -> int:
    return main(
        ("--config", str(device.config_path), *arguments),
        secret_store=secrets,
        stdout=stdout or StringIO(),
        stderr=StringIO(),
    )


def _configure_identity(root: Path) -> None:
    _git(root, "config", "user.name", "Codex Usage Acceptance")
    _git(root, "config", "user.email", "codex-usage@example.invalid")


def _git(root: Path | None, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if root is not None:
        command.extend(("-C", str(root)))
    command.extend(arguments)
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


if __name__ == "__main__":
    unittest.main()
