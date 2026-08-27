from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from codex_usage.domain.attribution import (
    ProjectAttribution,
    ProjectResolutionKind,
)
from codex_usage.domain.lineage import (
    ParentCandidate,
    ParentEvidenceSource,
    build_lineage,
)
from codex_usage.domain.token_usage import (
    CalculatedTokenEvent,
    Operation,
    RawTokenCheckpoint,
    TokenCounts,
)
from codex_usage.ledger.schema_validation import LedgerSchemaValidator
from codex_usage.privacy.encoder import UsageEncodingError, UsageEventEncoder
from codex_usage.privacy.guard import LedgerPrivacyGuard
from codex_usage.privacy.identifiers import project_id
from codex_usage.sources.codex_jsonl import RolloutMetadata


KEY = bytes(range(32))
DEVICE_ID = "00000000-0000-4000-8000-000000000001"
COUNTS = TokenCounts(10, 2, None, 2, 1, 12)


def metadata(thread_id: str = "child") -> RolloutMetadata:
    return RolloutMetadata(
        thread_id=thread_id,
        root_session_id="root",
        forked_from_id="fork-parent",
        source_kind="subAgentThreadSpawn",
        source_parent_thread_id="parent",
        cli_version="0.150.0",
        cwd="C:/raw/local/path",
        git_repository_url="https://github.com/private/repository.git",
        git_branch="secret-branch",
        git_commit_hash="commit",
    )


def calculated(
    *,
    thread_id: str = "child",
    turn_id: str | None = "raw-turn",
    occurred_at: datetime | None = None,
) -> CalculatedTokenEvent:
    checkpoint = RawTokenCheckpoint(
        rollout_thread_id=thread_id,
        rollout_forked_from_id="fork-parent",
        turn_id=turn_id,
        token_event_ordinal=0,
        record_index=3,
        occurred_at=occurred_at or datetime(2026, 8, 26, tzinfo=timezone.utc),
        operation=Operation.TURN,
        model="model",
        reasoning_effort="medium",
        cumulative=COUNTS,
        reported_last=COUNTS,
        activity_workdirs=("C:/raw/local/path",),
    )
    return CalculatedTokenEvent(checkpoint, COUNTS, ())


class UsageEventEncoderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.encoder = UsageEventEncoder(KEY, DEVICE_ID, parser_version="0.1.0")
        self.lineage = build_lineage(
            {"root", "parent", "child", "fork-parent"},
            (
                ParentCandidate(
                    "child",
                    "parent",
                    ParentEvidenceSource.SQLITE_SPAWN,
                ),
            ),
            {"child": "root"},
        )

    def test_raw_values_are_hmac_encoded_and_event_passes_guards(self) -> None:
        attribution = ProjectAttribution(
            "github.com/private/repository",
            ProjectResolutionKind.ACTIVITY_GIT,
            1,
        )

        encoded = self.encoder.encode(
            calculated(),
            attribution,
            metadata(),
            self.lineage,
        )

        encoded_event_id = encoded["event_id"]
        encoded_source_id = encoded["source_event_id"]
        self.assertIsInstance(encoded_event_id, str)
        self.assertIsInstance(encoded_source_id, str)
        assert isinstance(encoded_event_id, str)
        assert isinstance(encoded_source_id, str)
        self.assertTrue(encoded_event_id.startswith("evt_h1_"))
        self.assertTrue(encoded_source_id.startswith("src_h1_"))
        self.assertEqual(
            encoded["project_id"],
            project_id(KEY, "github.com/private/repository"),
        )
        serialized = repr(encoded)
        for raw_value in (
            "raw-turn",
            "C:/raw/local/path",
            "github.com/private/repository",
            "secret-branch",
        ):
            self.assertNotIn(raw_value, serialized)
        LedgerSchemaValidator.default().validate(encoded)
        LedgerPrivacyGuard().validate(encoded)

    def test_existing_manual_project_id_is_not_hashed_again(self) -> None:
        manual_project = project_id(KEY, "manual-project")
        attribution = ProjectAttribution(
            manual_project,
            ProjectResolutionKind.MANUAL,
            0,
        )

        encoded = self.encoder.encode(
            calculated(),
            attribution,
            metadata(),
            self.lineage,
        )

        self.assertEqual(encoded["project_id"], manual_project)

    def test_legacy_checkpoint_uses_stable_fallback_source_id(self) -> None:
        attribution = ProjectAttribution(
            None,
            ProjectResolutionKind.UNCLASSIFIED,
            0,
        )
        event = calculated(turn_id=None)

        first = self.encoder.encode(event, attribution, metadata(), self.lineage)
        second = self.encoder.encode(event, attribution, metadata(), self.lineage)

        self.assertEqual(first["source_event_id"], second["source_event_id"])
        self.assertIsNone(first["turn_key"])
        self.assertIsNone(first["project_id"])

    def test_mismatched_metadata_and_non_utc_time_are_rejected(self) -> None:
        attribution = ProjectAttribution(
            None,
            ProjectResolutionKind.UNCLASSIFIED,
            0,
        )
        with self.assertRaises(UsageEncodingError):
            self.encoder.encode(
                calculated(),
                attribution,
                metadata("different"),
                self.lineage,
            )

        non_utc = calculated(
            occurred_at=datetime(
                2026,
                8,
                26,
                tzinfo=timezone(timedelta(hours=9)),
            )
        )
        with self.assertRaises(UsageEncodingError):
            self.encoder.encode(non_utc, attribution, metadata(), self.lineage)


if __name__ == "__main__":
    unittest.main()
