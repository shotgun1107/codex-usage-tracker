from __future__ import annotations

import os
import unittest
import uuid

from codex_usage.privacy.identifiers import generate_shared_key
from codex_usage.secret_store import (
    SecretStoreUnavailable,
    WindowsCredentialStore,
)


@unittest.skipUnless(os.name == "nt", "Windows Credential Manager integration")
@unittest.skipIf(
    os.environ.get("CI", "").lower() == "true",
    "requires an interactive Windows user session",
)
class WindowsCredentialStoreIntegrationTests(unittest.TestCase):
    def test_temporary_secret_round_trip_and_cleanup(self) -> None:
        store = WindowsCredentialStore()
        target = f"CodexUsageTracker/tests/{uuid.uuid4()}"
        secret = generate_shared_key()
        try:
            self.assertIsNone(store.get(target))
            try:
                store.put(target, secret)
            except SecretStoreUnavailable as error:
                if error.error_code == 1312:
                    self.skipTest("test host has no interactive Windows logon session")
                raise
            self.assertEqual(store.get(target), secret)
        finally:
            store.delete(target)
        self.assertIsNone(store.get(target))


if __name__ == "__main__":
    unittest.main()
