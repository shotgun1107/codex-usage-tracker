from __future__ import annotations

from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_usage.application.project_management import (
    ProjectManagementError,
    ProjectManagementService,
)
from codex_usage.cli import main
from codex_usage.config import AppConfig, save_config
from codex_usage.ledger.jsonl import LedgerReader, LedgerWriter
from codex_usage.ledger.replay import replay_ledger_events
from codex_usage.privacy.identifiers import (
    generate_shared_key,
    key_id,
    thread_key,
    turn_key,
)
from codex_usage.secret_store import MemorySecretStore
from codex_usage.storage.sqlite import LocalStateStore
from tests.ledger_events import opaque, usage_event


DEVICE = "00000000-0000-4000-8000-000000000077"
RAW_UNRESOLVED_THREAD = "019f0000-0000-7000-8000-000000000077"
PROJECT_ONE = opaque("prj_h1_", "management-project-one")
PROJECT_TWO = opaque("prj_h1_", "management-project-two")


class ProjectManagementIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "ledger"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.shared_key = generate_shared_key()
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
        self._create_codex_state()
        self._seed_usage()
        self.service = ProjectManagementService(self.config, self.shared_key)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lists_projects_and_resolves_local_raw_thread_id(self) -> None:
        projects = self.service.list_projects()
        unresolved = self.service.list_unresolved()

        self.assertEqual(
            [(row.project_id, row.total_tokens) for row in projects],
            [(PROJECT_TWO, 30), (PROJECT_ONE, 12)],
        )
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].local_thread_id, RAW_UNRESOLVED_THREAD)
        self.assertEqual(unresolved[0].resolutions, ("unclassified",))
        self.assertEqual(unresolved[0].total_tokens, 20)

    def test_manual_link_is_private_idempotent_and_revisioned(self) -> None:
        first = self.service.link(
            subject_type="thread",
            subject=RAW_UNRESOLVED_THREAD,
            target_project_id=PROJECT_ONE,
        )
        repeated = self.service.link(
            subject_type="thread",
            subject=RAW_UNRESOLVED_THREAD,
            target_project_id=PROJECT_ONE,
        )
        revised = self.service.link(
            subject_type="thread",
            subject=RAW_UNRESOLVED_THREAD,
            target_project_id=PROJECT_TWO,
        )

        self.assertTrue(first.changed)
        self.assertEqual(first.revision, 1)
        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.event_id, first.event_id)
        self.assertTrue(revised.changed)
        self.assertEqual(revised.revision, 2)
        read = LedgerReader(self.ledger).read_all()
        replay = replay_ledger_events(read.events, expected_key_id=self.config.key_id)
        mapping = next(
            event
            for event in replay.mapping_events
            if event["kind"] == "manual_assignment"
        )
        self.assertEqual(mapping["target_project_id"], PROJECT_TWO)
        self.assertEqual(mapping["supersedes"], first.event_id)
        self.assertNotIn(
            RAW_UNRESOLVED_THREAD.encode("utf-8"),
            b"".join(path.read_bytes() for path in self.ledger.rglob("*.jsonl")),
        )

    def test_alias_combines_usage_and_rejects_cycle(self) -> None:
        result = self.service.alias(
            source_project_id=PROJECT_ONE,
            target_project_id=PROJECT_TWO,
        )
        repeated = self.service.alias(
            source_project_id=PROJECT_ONE,
            target_project_id=PROJECT_TWO,
        )

        self.assertTrue(result.changed)
        self.assertFalse(repeated.changed)
        projects = self.service.list_projects()
        self.assertEqual(
            [(row.project_id, row.total_tokens) for row in projects],
            [(PROJECT_TWO, 42)],
        )
        with self.assertRaisesRegex(ProjectManagementError, "cycle"):
            self.service.alias(
                source_project_id=PROJECT_TWO,
                target_project_id=PROJECT_ONE,
            )

    def test_unknown_subject_project_and_malformed_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ProjectManagementError, "thread does not exist"):
            self.service.link(
                subject_type="thread",
                subject="unknown-raw-thread",
                target_project_id=PROJECT_ONE,
            )
        with self.assertRaisesRegex(ProjectManagementError, "does not exist"):
            self.service.link(
                subject_type="thread",
                subject=RAW_UNRESOLVED_THREAD,
                target_project_id=opaque("prj_h1_", "unknown-project"),
            )
        with self.assertRaisesRegex(ProjectManagementError, "malformed"):
            self.service.link(
                subject_type="thread",
                subject="thr_h1_bad",
                target_project_id=PROJECT_ONE,
            )

    def test_cli_lists_unresolved_and_writes_manual_link(self) -> None:
        config_path = self.root / "config.json"
        save_config(self.config, config_path)
        secrets = MemorySecretStore()
        secrets.put(self.config.credential_target, self.shared_key)
        list_output = StringIO()
        unresolved_output = StringIO()
        link_output = StringIO()
        alias_output = StringIO()

        list_code = main(
            ("--config", str(config_path), "project", "list"),
            secret_store=secrets,
            stdout=list_output,
        )
        unresolved_code = main(
            ("--config", str(config_path), "project", "unresolved"),
            secret_store=secrets,
            stdout=unresolved_output,
        )
        link_code = main(
            (
                "--config",
                str(config_path),
                "project",
                "link",
                "--thread",
                RAW_UNRESOLVED_THREAD,
                "--project",
                PROJECT_ONE,
            ),
            secret_store=secrets,
            stdout=link_output,
        )
        alias_code = main(
            (
                "--config",
                str(config_path),
                "project",
                "alias",
                "--from",
                PROJECT_ONE,
                "--to",
                PROJECT_TWO,
            ),
            secret_store=secrets,
            stdout=alias_output,
        )

        self.assertEqual(
            (list_code, unresolved_code, link_code, alias_code),
            (0, 0, 0, 0),
        )
        self.assertIn(PROJECT_ONE, list_output.getvalue())
        self.assertIn(RAW_UNRESOLVED_THREAD, unresolved_output.getvalue())
        self.assertIn("프로젝트 연결 기록 완료", link_output.getvalue())
        self.assertIn("codex-usage sync", link_output.getvalue())
        self.assertIn("프로젝트 별칭 기록 완료", alias_output.getvalue())

    def _create_codex_state(self) -> None:
        connection = sqlite3.connect(self.codex_home / "state_5.sqlite")
        try:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
            connection.execute(
                "INSERT INTO threads (id) VALUES (?)",
                (RAW_UNRESOLVED_THREAD,),
            )
            connection.commit()
        finally:
            connection.close()

    def _seed_usage(self) -> None:
        unresolved_thread_key = thread_key(
            self.shared_key,
            RAW_UNRESOLVED_THREAD,
        )
        unresolved = usage_event(
            event_id=opaque("evt_h1_", "management-unresolved"),
            source_event_id=opaque("src_h1_", "management-unresolved"),
            device_id=DEVICE,
            project_id=None,
            thread_key=unresolved_thread_key,
            turn_key=turn_key(self.shared_key, "unresolved-turn"),
            total=20,
        )
        unresolved["project_resolution"] = "unclassified"
        events = (
            usage_event(
                event_id=opaque("evt_h1_", "management-one"),
                source_event_id=opaque("src_h1_", "management-one"),
                device_id=DEVICE,
                project_id=PROJECT_ONE,
                thread_key=thread_key(self.shared_key, "assigned-one"),
                turn_key=turn_key(self.shared_key, "assigned-one-turn"),
                total=12,
            ),
            usage_event(
                event_id=opaque("evt_h1_", "management-two"),
                source_event_id=opaque("src_h1_", "management-two"),
                device_id=DEVICE,
                project_id=PROJECT_TWO,
                thread_key=thread_key(self.shared_key, "assigned-two"),
                turn_key=turn_key(self.shared_key, "assigned-two-turn"),
                total=30,
            ),
            unresolved,
        )
        for event in events:
            event["key_id"] = self.config.key_id
        store = LocalStateStore(self.config.state_db)
        store.enqueue_outbox_events(events)
        writer = LedgerWriter(
            self.ledger,
            DEVICE,
            expected_key_id=self.config.key_id,
        )
        writer.flush(store, limit=100)
        replay = replay_ledger_events(
            LedgerReader(self.ledger).read_all().events,
            expected_key_id=self.config.key_id,
        )
        store.rebuild_read_model(replay)


if __name__ == "__main__":
    unittest.main()
