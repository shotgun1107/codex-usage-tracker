from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_usage.config import AppConfig, ConfigError, load_config, save_config
from codex_usage.privacy.identifiers import generate_shared_key, key_id
from codex_usage.secret_store import (
    MemorySecretStore,
    SecretStoreError,
    decode_recovery_key,
    encode_recovery_key,
)


DEVICE_ID = "00000000-0000-4000-8000-000000000001"


def config(root: Path, shared_key: bytes) -> AppConfig:
    return AppConfig(
        schema_version=1,
        device_id=DEVICE_ID,
        codex_home=str((root / "codex-home").resolve()),
        state_db=str((root / "state.sqlite").resolve()),
        ledger_root=str((root / "ledger").resolve()),
        credential_target=f"CodexUsageTracker/{key_id(shared_key)}",
        key_id=key_id(shared_key),
    )


class ConfigAndSecretTests(unittest.TestCase):
    def test_config_round_trip_contains_no_secret(self) -> None:
        shared_key = generate_shared_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            expected = config(root, shared_key)

            written = save_config(expected, path)
            loaded = load_config(path)

            self.assertEqual(written, path.resolve())
            self.assertEqual(loaded, expected)
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(encode_recovery_key(shared_key), raw)

    def test_config_refuses_accidental_overwrite_and_invalid_shapes(self) -> None:
        shared_key = generate_shared_key()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            expected = config(root, shared_key)
            save_config(expected, path)

            with self.assertRaises(ConfigError):
                save_config(expected, path)
            save_config(expected, path, overwrite=True)

            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

        with self.assertRaises(ConfigError):
            AppConfig(
                schema_version=1,
                device_id=DEVICE_ID,
                codex_home="relative",
                state_db="relative",
                ledger_root="relative",
                credential_target="target",
                key_id="key_h1_invalid",
            )

    def test_recovery_key_round_trip_and_validation(self) -> None:
        shared_key = generate_shared_key()
        encoded = encode_recovery_key(shared_key)

        self.assertEqual(decode_recovery_key(encoded), shared_key)
        with self.assertRaises(SecretStoreError):
            decode_recovery_key("invalid!")
        with self.assertRaises(SecretStoreError):
            encode_recovery_key(b"short")

    def test_memory_secret_store_has_same_port_semantics(self) -> None:
        shared_key = generate_shared_key()
        store = MemorySecretStore()

        self.assertIsNone(store.get("target"))
        store.put("target", shared_key)
        self.assertEqual(store.get("target"), shared_key)
        store.delete("target")
        self.assertIsNone(store.get("target"))

        with self.assertRaises(SecretStoreError):
            store.put("target", b"short")
        with self.assertRaises(SecretStoreError):
            store.get("")


if __name__ == "__main__":
    unittest.main()
