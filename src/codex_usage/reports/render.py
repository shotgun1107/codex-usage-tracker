"""Deterministic plain-text and Markdown usage report renderers."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unicodedata

from codex_usage.reports.query import ReportRow, TokenMetric, UsageReport


_TOKEN_COLUMNS = (
    ("input", "input_tokens"),
    ("cache", "cached_input_tokens"),
    ("cache_write", "cache_write_input_tokens"),
    ("output", "output_tokens"),
    ("reasoning", "reasoning_output_tokens"),
    ("total", "total_tokens"),
)


def render_terminal(report: UsageReport) -> str:
    lines = [
        f"기간: {_period(report)}  시간대: {report.timezone_name}",
        (
            f"총 토큰: {_metric(report.total.total_tokens)}  "
            f"포함 이벤트: {report.total.included_events:,}  "
            f"제외 이벤트: {report.total.excluded_events:,}"
        ),
        "",
    ]
    headers, rows = _table_values(report)
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _display_width(value))
    lines.append(
        "  ".join(_pad(header, widths[index]) for index, header in enumerate(headers))
    )
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append(
            "  ".join(
                _pad(
                    value,
                    widths[index],
                    right=index >= len(report.group_by),
                )
                for index, value in enumerate(row)
            )
        )
    if not rows:
        lines.append("조회 조건에 맞는 사용량이 없습니다.")
    if _has_partial(report):
        lines.extend(("", "* 일부 이벤트에서 해당 세부 토큰 필드가 제공되지 않았습니다."))
    return "\n".join(lines) + "\n"


def render_markdown(report: UsageReport) -> str:
    headers, rows = _table_values(report)
    lines = [
        "# Codex 사용량 보고서",
        "",
        f"- 기간: {_period(report)}",
        f"- 시간대: `{report.timezone_name}`",
        f"- 총 토큰: **{_metric(report.total.total_tokens)}**",
        f"- 포함 이벤트: {report.total.included_events:,}",
        f"- 제외 이벤트: {report.total.excluded_events:,}",
        "",
        "| " + " | ".join(_escape(value) for value in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape(value) for value in row) + " |")
    if not rows:
        lines.append(f"| {' | '.join('—' for _ in headers)} |")
    if _has_partial(report):
        lines.extend(("", r"\* 일부 이벤트에서 해당 세부 토큰 필드가 제공되지 않았습니다."))
    return "\n".join(lines) + "\n"


def write_markdown_report(path: str | Path, content: str) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return target
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _table_values(report: UsageReport) -> tuple[list[str], list[list[str]]]:
    headers = [
        *report.group_by,
        *(label for label, _ in _TOKEN_COLUMNS),
        "cumulative",
        "events",
        "excluded",
    ]
    rows = [
        [
            *(row.dimensions[dimension] for dimension in report.group_by),
            *(
                _metric(getattr(row.tokens, attribute))
                for _, attribute in _TOKEN_COLUMNS
            ),
            f"{row.cumulative_total_tokens:,}",
            f"{row.tokens.included_events:,}",
            f"{row.tokens.excluded_events:,}",
        ]
        for row in report.rows
    ]
    return headers, rows


def _metric(metric: TokenMetric) -> str:
    if metric.value is None:
        return "—"
    suffix = "*" if metric.partial else ""
    return f"{metric.value:,}{suffix}"


def _period(report: UsageReport) -> str:
    if report.from_date is None and report.to_date is None:
        return "전체"
    return f"{report.from_date or '처음'} ~ {report.to_date or '현재'}"


def _has_partial(report: UsageReport) -> bool:
    summaries = (report.total, *(row.tokens for row in report.rows))
    return any(
        getattr(summary, attribute).partial
        for summary in summaries
        for _, attribute in _TOKEN_COLUMNS
    )


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _display_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in value
    )


def _pad(value: str, width: int, *, right: bool = False) -> str:
    padding = " " * max(0, width - _display_width(value))
    return padding + value if right else value + padding
