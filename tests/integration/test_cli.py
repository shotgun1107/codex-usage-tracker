from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest

from codex_usage.cli import main
from codex_usage.config import load_config
from codex_usage.privacy.identifiers import generate_shared_key
from codex_usage.secret_store import (
    MemorySecretStore,
    encode_recovery_key,
)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lifecycle" / "parent.jsonl"


class CliIntegrationTests(unittest.TestCase):
    def test_init_collect_and_doctor_user_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            codex_home = root / "codex-home"
            rollout = codex_home / "sessions" / "2026" / "rollout.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_bytes(FIXTURE.read_bytes())
            ledger = root / "ledger"
            secrets = MemorySecretStore()
            init_output = StringIO()

            init_code = main(
                (
                    "--config",
                    str(config_path),
                    "init",
                    "--ledger",
                    str(ledger),
                    "--codex-home",
                    str(codex_home),
                ),
                secret_store=secrets,
                stdout=init_output,
            )

            self.assertEqual(init_code, 0)
            config = load_config(config_path)
            shared_key = secrets.get(config.credential_target)
            self.assertIsNotNone(shared_key)
            assert shared_key is not None
            self.assertIn(encode_recovery_key(shared_key), init_output.getvalue())
            self.assertTrue(Path(config.state_db).is_file())

            collect_output = StringIO()
            collect_code = main(
                ("--config", str(config_path), "collect"),
                secret_store=secrets,
                stdout=collect_output,
            )

            self.assertEqual(collect_code, 0)
            self.assertIn("신규 이벤트 4", collect_output.getvalue())

            doctor_output = StringIO()
            doctor_code = main(
                ("--config", str(config_path), "doctor"),
                secret_store=secrets,
                stdout=doctor_output,
            )

            self.assertEqual(doctor_code, 0)
            self.assertIn("[OK] shared-key", doctor_output.getvalue())
            self.assertIn("[경고] ledger-git", doctor_output.getvalue())

    def test_init_can_import_existing_recovery_key_without_printing_it(self) -> None:
        shared_key = generate_shared_key()
        recovery_key = encode_recovery_key(shared_key)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            output = StringIO()
            secrets = MemorySecretStore()

            code = main(
                (
                    "--config",
                    str(config_path),
                    "init",
                    "--ledger",
                    str(root / "ledger"),
                    "--codex-home",
                    str(root / "codex-home"),
                    "--import-key",
                ),
                secret_store=secrets,
                secret_reader=lambda: recovery_key,
                stdout=output,
            )

            self.assertEqual(code, 0)
            config = load_config(config_path)
            self.assertEqual(secrets.get(config.credential_target), shared_key)
            self.assertNotIn(recovery_key, output.getvalue())

    def test_expected_failure_is_reported_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            error_output = StringIO()

            code = main(
                ("--config", str(root / "missing.json"), "collect"),
                secret_store=MemorySecretStore(),
                stderr=error_output,
            )

            self.assertEqual(code, 2)
            self.assertIn("오류:", error_output.getvalue())
            self.assertNotIn("Traceback", error_output.getvalue())


if __name__ == "__main__":
    unittest.main()
