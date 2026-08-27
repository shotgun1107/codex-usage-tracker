from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from codex_usage.application.sync import SyncService
from codex_usage.application.project_management import ProjectManagementService
from codex_usage.cli import main
from codex_usage.config import AppConfig, save_config
from codex_usage.ledger.jsonl import LedgerReader, ledger_relative_path
from codex_usage.privacy.identifiers import generate_shared_key, key_id
from codex_usage.secret_store import MemorySecretStore
from codex_usage.storage.sqlite import LocalStateStore
from codex_usage.sync.git import GitSyncError
from tests.ledger_events import PROJECT_ORIGINAL, opaque, usage_event


DEVICE_ONE = "00000000-0000-4000-8000-000000000011"
DEVICE_TWO = "00000000-0000-4000-8000-000000000022"


@unittest.skipUnless(shutil.which("git"), "Git is required")
class GitSyncIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
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

        self.checkout = self.root / "device-one"
        _git(None, "clone", str(self.remote), str(self.checkout))
        _configure_identity(self.checkout)
        self.shared_key = generate_shared_key()
        self.config = _config(
            self.root,
            self.checkout,
            DEVICE_ONE,
            self.shared_key,
            state_name="device-one.sqlite",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_sync_commits_pushes_and_rebuilds_read_model(self) -> None:
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "one", total=12),
        )

        result = SyncService(self.config, self.shared_key).sync()

        self.assertTrue(result.commit_created)
        self.assertTrue(result.pushed)
        self.assertEqual(result.ledger_event_count, 1)
        self.assertEqual(LocalStateStore(self.config.state_db).read_model_counts().usage, 1)
        verification = self.root / "verification"
        _git(None, "clone", str(self.remote), str(verification))
        self.assertEqual(len(tuple(verification.rglob("*.jsonl"))), 1)

    def test_sync_pulls_another_device_and_combines_usage(self) -> None:
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "one", total=12),
        )
        SyncService(self.config, self.shared_key).sync()

        second = self.root / "device-two"
        _git(None, "clone", str(self.remote), str(second))
        _configure_identity(second)
        _append_event(
            second,
            _event(self.shared_key, DEVICE_TWO, "two", total=20),
        )
        _git(second, "add", "devices")
        _git(second, "commit", "-m", "Second device usage")
        _git(second, "push", "origin", "main")

        result = SyncService(self.config, self.shared_key).sync()

        self.assertFalse(result.commit_created)
        self.assertFalse(result.pushed)
        self.assertEqual(result.ledger_event_count, 2)
        rows = LocalStateStore(self.config.state_db).daily_usage_utc()
        self.assertEqual(sum(row.total_tokens or 0 for row in rows), 32)

    def test_disjoint_simultaneous_device_changes_rebase_and_push(self) -> None:
        second = self.root / "device-two"
        _git(None, "clone", str(self.remote), str(second))
        _configure_identity(second)
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "local-concurrent", total=12),
        )
        _append_event(
            second,
            _event(self.shared_key, DEVICE_TWO, "remote-concurrent", total=20),
        )
        _git(second, "add", "devices")
        _git(second, "commit", "-m", "Concurrent remote usage")
        _git(second, "push", "origin", "main")

        result = SyncService(self.config, self.shared_key).sync()

        self.assertTrue(result.commit_created)
        self.assertTrue(result.rebased)
        self.assertTrue(result.pushed)
        self.assertEqual(result.ledger_event_count, 2)
        self.assertEqual(LocalStateStore(self.config.state_db).read_model_counts().usage, 2)

    def test_sync_rejects_changes_outside_own_device_directory(self) -> None:
        (self.checkout / "README.md").write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(
            GitSyncError,
            "change_outside_device_directory",
        ):
            SyncService(self.config, self.shared_key).sync()

    def test_sync_rejects_rewriting_committed_ledger_history(self) -> None:
        first = _event(self.shared_key, DEVICE_ONE, "one", total=12)
        path = _append_event(self.checkout, first)
        SyncService(self.config, self.shared_key).sync()
        replacement = _event(self.shared_key, DEVICE_ONE, "replacement", total=99)
        path.write_text(_canonical(replacement) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(
            GitSyncError,
            "ledger_history_rewrite_forbidden",
        ):
            SyncService(self.config, self.shared_key).sync()

    def test_sync_rejects_an_unapproved_outgoing_commit(self) -> None:
        (self.checkout / "README.md").write_text("manually changed\n", encoding="utf-8")
        _git(self.checkout, "add", "README.md")
        _git(self.checkout, "commit", "-m", "Unapproved local commit")

        with self.assertRaisesRegex(
            GitSyncError,
            "outgoing_change_outside_device_directory",
        ):
            SyncService(self.config, self.shared_key).sync()

    def test_fetch_failure_preserves_the_new_local_commit(self) -> None:
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "one", total=12),
        )
        SyncService(self.config, self.shared_key).sync()
        previous_head = _git(self.checkout, "rev-parse", "HEAD").stdout.strip()
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "two", total=20),
        )
        _git(
            self.checkout,
            "remote",
            "set-url",
            "origin",
            str(self.root / "missing.git"),
        )

        with self.assertRaisesRegex(GitSyncError, "git_fetch_failed"):
            SyncService(self.config, self.shared_key).sync()

        self.assertNotEqual(_git(self.checkout, "rev-parse", "HEAD").stdout.strip(), previous_head)
        self.assertEqual(_git(self.checkout, "status", "--porcelain").stdout, "")

    def test_cli_sync_reports_the_result(self) -> None:
        _append_event(
            self.checkout,
            _event(self.shared_key, DEVICE_ONE, "cli", total=30),
        )
        config_path = self.root / "config.json"
        save_config(self.config, config_path)
        secrets = MemorySecretStore()
        secrets.put(self.config.credential_target, self.shared_key)
        output = StringIO()

        code = main(
            ("--config", str(config_path), "sync"),
            secret_store=secrets,
            stdout=output,
        )

        self.assertEqual(code, 0)
        self.assertIn("동기화 완료", output.getvalue())
        self.assertIn("통합 장부 이벤트 1", output.getvalue())

    def test_manual_project_mapping_is_committed_and_pushed(self) -> None:
        assigned = _event(self.shared_key, DEVICE_ONE, "assigned", total=12)
        unresolved = _event(self.shared_key, DEVICE_ONE, "unresolved", total=20)
        unresolved["project_id"] = None
        unresolved["project_resolution"] = "unclassified"
        _append_event(self.checkout, assigned)
        _append_event(self.checkout, unresolved)
        SyncService(self.config, self.shared_key).sync()

        mapping = ProjectManagementService(
            self.config,
            self.shared_key,
        ).link(
            subject_type="thread",
            subject=str(unresolved["thread_key"]),
            target_project_id=PROJECT_ORIGINAL,
        )
        result = SyncService(self.config, self.shared_key).sync()

        self.assertTrue(mapping.changed)
        self.assertTrue(result.commit_created)
        self.assertTrue(result.pushed)
        verification = self.root / "mapping-verification"
        _git(None, "clone", str(self.remote), str(verification))
        events = LedgerReader(verification).read_all().events
        self.assertEqual(
            sum(event.get("event_type") == "mapping" for event in events),
            1,
        )


def _config(
    root: Path,
    checkout: Path,
    device_id: str,
    shared_key: bytes,
    *,
    state_name: str,
) -> AppConfig:
    current_key_id = key_id(shared_key)
    return AppConfig(
        schema_version=1,
        device_id=device_id,
        codex_home=str((root / "codex-home").resolve()),
        state_db=str((root / state_name).resolve()),
        ledger_root=str(checkout.resolve()),
        credential_target=f"CodexUsageTracker/{current_key_id}",
        key_id=current_key_id,
    )


def _event(
    shared_key: bytes,
    device_id: str,
    label: str,
    *,
    total: int,
) -> dict[str, object]:
    event = usage_event(
        event_id=opaque("evt_h1_", f"event-{label}"),
        source_event_id=opaque("src_h1_", f"source-{label}"),
        device_id=device_id,
        thread_key=opaque("thr_h1_", f"thread-{label}"),
        turn_key=opaque("turn_h1_", f"turn-{label}"),
        total=total,
    )
    event["key_id"] = key_id(shared_key)
    return event


def _append_event(root: Path, event: dict[str, object]) -> Path:
    relative = ledger_relative_path(event)
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(_canonical(event) + "\n")
    return path


def _canonical(event: dict[str, object]) -> str:
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
