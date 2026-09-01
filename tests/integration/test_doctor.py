from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

from codex_usage.application.collect import CollectService
from codex_usage.application.doctor import CheckStatus, run_doctor
from codex_usage.application.sync import SyncService
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerReader, LedgerWriter
from codex_usage.ledger.replay import replay_ledger_events
from codex_usage.privacy.identifiers import generate_shared_key, key_id
from codex_usage.storage.sqlite import (
    LocalStateStore,
    ParserIssueRecord,
)
from codex_usage.sync.git import GitSyncError
from tests.ledger_events import opaque, usage_event


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle" / "parent.jsonl"
DEVICE = "00000000-0000-4000-8000-000000000088"


@unittest.skipUnless(shutil.which("git"), "Git is required")
class DoctorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.shared_key = generate_shared_key()
        self.remote = self.root / "remote.git"
        self._create_remote()
        self.ledger = self.root / "ledger"
        _git(None, "clone", str(self.remote), str(self.ledger))
        _configure_identity(self.ledger)
        self.codex_home = self.root / "codex-home"
        rollout = self.codex_home / "sessions" / "2026" / "rollout.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_bytes(FIXTURE.read_bytes())
        self._create_codex_state(rollout)
        current_key_id = key_id(self.shared_key)
        self.config = AppConfig(
            schema_version=1,
            device_id=DEVICE,
            codex_home=str(self.codex_home.resolve()),
            state_db=str((self.root / "state.sqlite").resolve()),
            ledger_root=str(self.ledger.resolve()),
            credential_target=f"CodexUsageTracker/{current_key_id}",
            key_id=current_key_id,
        )
        CollectService(self.config, self.shared_key).collect()
        SyncService(self.config, self.shared_key).sync()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_healthy_device_reports_every_core_check_as_ok(self) -> None:
        result = run_doctor(self.config, self.shared_key)
        checks = {check.name: check for check in result.checks}

        expected = {
            "shared-key",
            "codex-rollouts",
            "codex-state",
            "codex-lineage",
            "codex-versions",
            "codex-join",
            "local-state",
            "collection-history",
            "sync-history",
            "outbox",
            "parser-issues",
            "classification",
            "ledger",
            "ledger-git",
            "ledger-remote",
            "read-model",
        }
        self.assertTrue(expected.issubset(checks))
        self.assertTrue(all(checks[name].status is CheckStatus.OK for name in expected))
        self.assertFalse(result.has_errors)

    def test_join_parser_classification_and_pending_sync_are_visible(self) -> None:
        extra = self.codex_home / "archived_sessions" / "extra.jsonl"
        extra.parent.mkdir()
        extra.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "json-only-thread",
                        "session_id": "json-only-thread",
                        "source": "cli",
                        "cli_version": "0.999.0-test",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        event = usage_event(
            event_id=opaque("evt_h1_", "doctor-unresolved"),
            source_event_id=opaque("src_h1_", "doctor-unresolved"),
            device_id=DEVICE,
            project_id=None,
            total=15,
        )
        event["key_id"] = self.config.key_id
        event["project_resolution"] = "ambiguous_multi_repo"
        store = LocalStateStore(self.config.state_db)
        store.enqueue_outbox_events((event,))
        store.record_parser_issues(
            (
                ParserIssueRecord(
                    "local-source",
                    "fatal_rollout_parse_error",
                    4,
                ),
            )
        )
        LedgerWriter(
            self.ledger,
            DEVICE,
            expected_key_id=self.config.key_id,
        ).flush(store, limit=100)
        replay = replay_ledger_events(
            LedgerReader(self.ledger).read_all().events,
            expected_key_id=self.config.key_id,
        )
        store.rebuild_read_model(replay)

        checks = {check.name: check for check in run_doctor(self.config, self.shared_key).checks}

        self.assertIs(checks["codex-join"].status, CheckStatus.WARNING)
        self.assertIn("JSONL-only threads 1", checks["codex-join"].message)
        self.assertIs(checks["parser-issues"].status, CheckStatus.WARNING)
        self.assertIn("fatal_rollout_parse_error=1", checks["parser-issues"].message)
        self.assertIs(checks["classification"].status, CheckStatus.WARNING)
        self.assertIn("ambiguous_multi_repo=1", checks["classification"].message)
        self.assertIs(checks["ledger-git"].status, CheckStatus.WARNING)

    def test_remote_access_failure_is_an_error_without_leaking_url(self) -> None:
        missing_remote = self.root / "private-missing-remote.git"
        _git(self.ledger, "remote", "set-url", "origin", str(missing_remote))
        with self.assertRaises(GitSyncError):
            SyncService(self.config, self.shared_key).sync()

        result = run_doctor(self.config, self.shared_key)
        checks = {check.name: check for check in result.checks}

        self.assertIs(checks["ledger-remote"].status, CheckStatus.ERROR)
        self.assertIs(checks["sync-history"].status, CheckStatus.WARNING)
        self.assertIn("last success at", checks["sync-history"].message)
        self.assertTrue(result.has_errors)
        self.assertNotIn(
            str(missing_remote),
            "\n".join(check.message for check in result.checks),
        )

    def _create_remote(self) -> None:
        _git(None, "init", "--bare", str(self.remote))
        seed = self.root / "seed"
        seed.mkdir()
        _git(seed, "init")
        _configure_identity(seed)
        _git(seed, "branch", "-M", "main")
        (seed / "README.md").write_text("private ledger\n", encoding="utf-8")
        _git(seed, "add", "README.md")
        _git(seed, "commit", "-m", "Initialize ledger")
        _git(seed, "remote", "add", "origin", str(self.remote))
        _git(seed, "push", "-u", "origin", "main")
        _git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")

    def _create_codex_state(self, rollout: Path) -> None:
        database = self.codex_home / "state_5.sqlite"
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)"
            )
            connection.execute(
                "INSERT INTO threads (id, rollout_path) VALUES (?, ?)",
                ("parent-thread", str(rollout)),
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
            connection.commit()
        finally:
            connection.close()


def _configure_identity(root: Path) -> None:
    _git(root, "config", "user.name", "Codex Usage Test")
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
