from __future__ import annotations

from copy import deepcopy
import unittest

from codex_usage.ledger.schema_validation import (
    LedgerSchemaError,
    LedgerSchemaValidator,
)
from codex_usage.privacy.guard import LedgerPrivacyGuard, PrivacyViolation
from tests.ledger_events import mapping_event, opaque, quota_event, usage_event


class LedgerSchemaValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.validator = LedgerSchemaValidator.default()

    def test_all_three_event_types_match_checked_in_schema(self) -> None:
        for event in (usage_event(), mapping_event(), quota_event()):
            self.validator.validate(event)

    def test_missing_extra_wrong_format_and_duplicate_flags_are_rejected(self) -> None:
        missing = usage_event()
        missing.pop("device_id")
        extra = dict(usage_event(), cwd="C:/private")
        wrong_uuid = dict(usage_event(), device_id="not-a-uuid")
        duplicate_flags = dict(usage_event(), flags=["one", "one"])

        for event in (missing, extra, wrong_uuid, duplicate_flags):
            with self.assertRaises(LedgerSchemaError):
                self.validator.validate(event)

    def test_nested_token_and_quota_bounds_are_rejected(self) -> None:
        negative = deepcopy(usage_event())
        negative["delta"]["total_tokens"] = -1  # type: ignore[index]
        excessive_quota = dict(quota_event(), used_percent=101.0)

        with self.assertRaises(LedgerSchemaError):
            self.validator.validate(negative)
        with self.assertRaises(LedgerSchemaError):
            self.validator.validate(excessive_quota)

    def test_error_does_not_echo_sensitive_field_value(self) -> None:
        event = dict(usage_event(), device_id="sensitive-value")

        with self.assertRaises(LedgerSchemaError) as raised:
            self.validator.validate(event)

        self.assertNotIn("sensitive-value", str(raised.exception))


class LedgerPrivacyGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guard = LedgerPrivacyGuard()

    def test_valid_pseudonymous_events_pass(self) -> None:
        for event in (usage_event(), mapping_event(), quota_event()):
            self.guard.validate(event)

    def test_raw_identifier_path_remote_and_forbidden_field_are_rejected(self) -> None:
        raw_project = dict(usage_event(), project_id="company/project")
        fake_prefixed_project = dict(usage_event(), project_id="prj_h1_secret")
        windows_path = dict(usage_event(), model="C:\\private\\repo")
        network_remote = mapping_event(
            display_value="https://github.com/company/private.git"
        )
        forbidden_field = dict(usage_event(), workdir="C:/private")
        control_character = mapping_event(display_value="unsafe\nname")

        for event in (
            raw_project,
            fake_prefixed_project,
            windows_path,
            network_remote,
            forbidden_field,
            control_character,
        ):
            with self.assertRaises(PrivacyViolation):
                self.guard.validate(event)

    def test_mapping_subject_requires_type_specific_pseudonymous_id(self) -> None:
        wrong_thread = mapping_event(
            subject_id=opaque("prj_h1_", "wrong-type")
        )
        local_mapping = mapping_event(
            kind="local_repo_link",
            subject_type="local_repo",
            subject_id=opaque("local_h1_", "repository"),
        )
        device_mapping = mapping_event(
            kind="device_name",
            subject_type="device",
            subject_id="00000000-0000-4000-8000-000000000001",
            target_project_id=None,
            display_value="집 컴퓨터",
        )

        with self.assertRaises(PrivacyViolation):
            self.guard.validate(wrong_thread)
        self.guard.validate(local_mapping)
        self.guard.validate(device_mapping)

    def test_error_does_not_echo_sensitive_value(self) -> None:
        event = dict(usage_event(), model="C:\\secret\\project")

        with self.assertRaises(PrivacyViolation) as raised:
            self.guard.validate(event)

        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
