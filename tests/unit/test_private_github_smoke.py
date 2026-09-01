from __future__ import annotations

import unittest

from codex_usage.ledger.schema_validation import LedgerSchemaValidator
from codex_usage.privacy.guard import LedgerPrivacyGuard
from codex_usage.privacy.identifiers import generate_shared_key
from scripts.private_github_smoke import (
    github_repository_name,
    synthetic_event,
)


class PrivateGithubSmokeTests(unittest.TestCase):
    def test_remote_parser_accepts_only_one_https_github_repository(self) -> None:
        self.assertEqual(
            github_repository_name(
                "https://github.com/shotgun1107/private-ledger.git"
            ),
            "shotgun1107/private-ledger",
        )
        for invalid in (
            "http://github.com/owner/repo",
            "https://example.com/owner/repo",
            "https://github.com/owner/repo/extra",
            "git@github.com:owner/repo.git",
            "https://token@github.com/owner/repo.git",
            "https://github.com/owner/repo.git?token=secret",
        ):
            with self.subTest(remote=invalid):
                with self.assertRaises(ValueError):
                    github_repository_name(invalid)

    def test_synthetic_event_passes_schema_and_privacy_guards(self) -> None:
        event = synthetic_event(
            generate_shared_key(),
            "00000000-0000-4000-8000-000000000094",
        )

        LedgerSchemaValidator.default().validate(event)
        LedgerPrivacyGuard().validate(event)


if __name__ == "__main__":
    unittest.main()
