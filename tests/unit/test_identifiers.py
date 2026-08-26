from __future__ import annotations

import base64
import hashlib
import hmac
import unittest

from codex_usage.privacy.identifiers import (
    IdentifierError,
    generate_shared_key,
    key_id,
    project_id,
    source_event_id,
    thread_key,
    turn_key,
)


class IdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = b"k" * 32

    def test_project_id_uses_documented_domain_and_urlsafe_encoding(self) -> None:
        remote = "github.com/example/repository"
        digest = hmac.digest(
            self.key,
            f"project:v1:{remote}".encode(),
            "sha256",
        )
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

        self.assertEqual(project_id(self.key, remote), f"prj_h1_{expected}")

    def test_identifiers_are_deterministic_and_domain_separated(self) -> None:
        raw_id = "019f0000-0000-7000-8000-000000000001"

        self.assertEqual(thread_key(self.key, raw_id), thread_key(self.key, raw_id))
        self.assertNotEqual(thread_key(self.key, raw_id), turn_key(self.key, raw_id))
        self.assertNotEqual(project_id(self.key, raw_id), thread_key(self.key, raw_id))

    def test_source_event_id_changes_with_ordinal(self) -> None:
        raw_turn_id = "019f0000-0000-7000-8000-000000000002"

        first = source_event_id(self.key, raw_turn_id, 0)
        second = source_event_id(self.key, raw_turn_id, 1)

        self.assertTrue(first.startswith("src_h1_"))
        self.assertNotEqual(first, second)

    def test_key_id_is_shorter_than_full_identifiers(self) -> None:
        identifier = key_id(self.key)

        self.assertTrue(identifier.startswith("key_h1_"))
        self.assertEqual(len(identifier.removeprefix("key_h1_")), 22)
        self.assertLess(len(identifier), len(thread_key(self.key, "thread")))

    def test_raw_values_do_not_appear_in_output(self) -> None:
        raw = "private-company/secret-project"

        self.assertNotIn(raw, project_id(self.key, raw))

    def test_generate_shared_key_meets_minimum_length(self) -> None:
        generated = generate_shared_key()

        self.assertEqual(len(generated), hashlib.sha256().digest_size)
        self.assertNotEqual(generated, generate_shared_key())

    def test_generate_shared_key_rejects_invalid_lengths(self) -> None:
        with self.assertRaises(IdentifierError):
            generate_shared_key(31)
        with self.assertRaises(IdentifierError):
            generate_shared_key(True)
        with self.assertRaises(IdentifierError):
            generate_shared_key("32")  # type: ignore[arg-type]

    def test_short_or_non_bytes_keys_are_rejected(self) -> None:
        with self.assertRaises(IdentifierError):
            project_id(b"short", "github.com/example/repo")
        with self.assertRaises(IdentifierError):
            project_id("k" * 32, "github.com/example/repo")  # type: ignore[arg-type]

    def test_invalid_values_and_ordinals_are_rejected(self) -> None:
        with self.assertRaises(IdentifierError):
            thread_key(self.key, "")
        with self.assertRaises(IdentifierError):
            turn_key(self.key, " padded ")
        with self.assertRaises(IdentifierError):
            source_event_id(self.key, "turn", -1)
        with self.assertRaises(IdentifierError):
            source_event_id(self.key, "turn", True)


if __name__ == "__main__":
    unittest.main()
