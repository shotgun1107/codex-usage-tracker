from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

from codex_usage.ledger.replay import replay_ledger_events
from codex_usage.reports.query import ReportError, ReportQuery, build_usage_report
from codex_usage.reports.render import (
    render_markdown,
    render_terminal,
    write_markdown_report,
)
from codex_usage.storage.sqlite import LocalStateStore
from tests.ledger_events import DEVICE_ID, mapping_event, opaque, usage_event


PROJECT_ALPHA = opaque("prj_h1_", "project-alpha")
PROJECT_BETA = opaque("prj_h1_", "project-beta")
DEVICE_TWO = "00000000-0000-4000-8000-000000000002"


def build_database(path: Path) -> LocalStateStore:
    first = usage_event(
        event_id=opaque("evt_h1_", "report-one"),
        source_event_id=opaque("src_h1_", "report-one"),
        project_id=PROJECT_ALPHA,
        occurred_at="2026-08-25T15:30:00Z",
        total=10,
    )
    first["delta"]["cache_write_input_tokens"] = 5  # type: ignore[index]
    events = (
        first,
        usage_event(
            event_id=opaque("evt_h1_", "report-two"),
            source_event_id=opaque("src_h1_", "report-two"),
            project_id=PROJECT_ALPHA,
            occurred_at="2026-08-26T14:59:00Z",
            total=20,
        ),
        usage_event(
            event_id=opaque("evt_h1_", "report-three"),
            source_event_id=opaque("src_h1_", "report-three"),
            project_id=PROJECT_ALPHA,
            occurred_at="2026-08-26T15:01:00Z",
            total=30,
        ),
        usage_event(
            event_id=opaque("evt_h1_", "report-four"),
            source_event_id=opaque("src_h1_", "report-four"),
            device_id=DEVICE_TWO,
            project_id=PROJECT_BETA,
            occurred_at="2026-08-26T10:00:00Z",
            model="other-model",
            total=40,
        ),
        usage_event(
            event_id=opaque("evt_h1_", "report-excluded"),
            source_event_id=opaque("src_h1_", "report-excluded"),
            project_id=PROJECT_ALPHA,
            occurred_at="2026-08-26T16:00:00Z",
            total=50,
        ),
        mapping_event(
            event_id=opaque("map_h1_", "project-name"),
            kind="project_name",
            subject_type="project",
            subject_id=PROJECT_ALPHA,
            target_project_id=None,
            display_value="알파|프로젝트",
        ),
        mapping_event(
            event_id=opaque("map_h1_", "device-name"),
            kind="device_name",
            subject_type="device",
            subject_id=DEVICE_ID,
            target_project_id=None,
            display_value="집 PC",
        ),
    )
    events[4]["delta"] = None  # type: ignore[index]
    store = LocalStateStore(path)
    store.rebuild_read_model(replay_ledger_events(events))
    return store


class UsageReportTests(unittest.TestCase):
    def test_kst_grouping_cumulative_partial_and_excluded_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            build_database(path)

            report = build_usage_report(path, ReportQuery())

            self.assertEqual(report.total.total_tokens.value, 100)
            self.assertEqual(report.total.included_events, 4)
            self.assertEqual(report.total.excluded_events, 1)
            alpha_rows = [
                row
                for row in report.rows
                if row.dimensions["project"] == "알파|프로젝트"
            ]
            self.assertEqual(
                [(row.dimensions["date"], row.tokens.total_tokens.value) for row in alpha_rows],
                [("2026-08-26", 30), ("2026-08-27", 30)],
            )
            self.assertEqual(
                [row.cumulative_total_tokens for row in alpha_rows],
                [30, 60],
            )
            self.assertTrue(alpha_rows[0].tokens.cache_write_input_tokens.partial)
            self.assertEqual(alpha_rows[1].tokens.excluded_events, 1)

    def test_date_project_model_device_and_source_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            build_database(path)

            day = build_usage_report(
                path,
                ReportQuery(
                    from_date=date(2026, 8, 26),
                    to_date=date(2026, 8, 26),
                ),
            )
            alpha = build_usage_report(
                path,
                ReportQuery(project="알파|프로젝트", group_by=("project",)),
            )
            other_model = build_usage_report(
                path,
                ReportQuery(model="other-model", group_by=("model", "device")),
            )
            source = build_usage_report(
                path,
                ReportQuery(source="vscode", device="집 PC", group_by=("source",)),
            )

            self.assertEqual(day.total.total_tokens.value, 70)
            self.assertEqual(alpha.total.total_tokens.value, 60)
            self.assertEqual(other_model.total.total_tokens.value, 40)
            self.assertEqual(source.total.total_tokens.value, 60)

    def test_renderers_and_atomic_markdown_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state.sqlite"
            build_database(path)
            report = build_usage_report(path, ReportQuery())

            terminal = render_terminal(report)
            markdown = render_markdown(report)
            output = write_markdown_report(root / "reports" / "usage.md", markdown)

            self.assertIn("총 토큰: 100", terminal)
            self.assertIn("cumulative", terminal)
            self.assertIn("알파\\|프로젝트", markdown)
            self.assertIn("일부 이벤트", markdown)
            self.assertEqual(output.read_text(encoding="utf-8"), markdown)

    def test_invalid_query_and_missing_database_fail(self) -> None:
        with self.assertRaises(ReportError):
            ReportQuery(
                from_date=date(2026, 8, 27),
                to_date=date(2026, 8, 26),
            )
        with self.assertRaises(ReportError):
            ReportQuery(group_by=("project", "project"))
        with self.assertRaises(ReportError):
            ReportQuery(group_by=("cost",))
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ReportError):
                build_usage_report(Path(directory) / "missing.sqlite", ReportQuery())


if __name__ == "__main__":
    unittest.main()
