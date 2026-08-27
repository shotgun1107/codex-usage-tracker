"""User-facing collection, sync, reporting, and project commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
import getpass
from pathlib import Path
import sqlite3
import sys
from typing import TextIO
import unicodedata
import uuid
from zoneinfo import ZoneInfo

from codex_usage import __version__
from codex_usage.application.collect import CollectError, CollectService
from codex_usage.application.doctor import CheckStatus, run_doctor
from codex_usage.application.lock import ApplicationLockError
from codex_usage.application.project_management import (
    MappingWriteResult,
    ProjectManagementError,
    ProjectManagementService,
)
from codex_usage.application.sync import SyncError, SyncService
from codex_usage.config import (
    AppConfig,
    ConfigError,
    default_config_path,
    load_config,
    save_config,
)
from codex_usage.ledger.jsonl import LedgerIoError
from codex_usage.ledger.replay import LedgerReplayError
from codex_usage.ledger.schema_validation import LedgerSchemaError
from codex_usage.privacy.guard import PrivacyViolation
from codex_usage.privacy.identifiers import generate_shared_key, key_id
from codex_usage.reports.query import ReportError, ReportQuery, build_usage_report
from codex_usage.reports.render import (
    render_markdown,
    render_terminal,
    write_markdown_report,
)
from codex_usage.secret_store import (
    SecretStore,
    SecretStoreError,
    decode_recovery_key,
    default_secret_store,
    encode_recovery_key,
)
from codex_usage.storage.sqlite import LocalStateStore, LocalStoreError
from codex_usage.sync.git import GitSyncError


_EXPECTED_ERRORS = (
    CollectError,
    ApplicationLockError,
    ConfigError,
    LedgerIoError,
    LedgerReplayError,
    LedgerSchemaError,
    LocalStoreError,
    PrivacyViolation,
    ProjectManagementError,
    ReportError,
    SecretStoreError,
    SyncError,
    GitSyncError,
)


def main(
    argv: Sequence[str] | None = None,
    *,
    secret_store: SecretStore | None = None,
    secret_reader: Callable[[], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    parser = _build_parser()
    arguments = parser.parse_args(argv)

    try:
        config_path = Path(arguments.config).expanduser().resolve()
        if arguments.command == "init":
            secrets = secret_store or default_secret_store()
            return _run_init(
                arguments,
                config_path,
                secrets,
                secret_reader or _read_recovery_key,
                output,
            )

        config = load_config(config_path)
        if arguments.command == "report":
            return _run_report(arguments, config, output)
        secrets = secret_store or default_secret_store()
        shared_key = secrets.get(config.credential_target)
        if shared_key is None:
            raise SecretStoreError("shared key is missing from Credential Manager")
        if arguments.command == "collect":
            return _run_collect(config, shared_key, output)
        if arguments.command == "sync":
            return _run_sync(config, shared_key, output)
        if arguments.command == "doctor":
            return _run_doctor(config, shared_key, output)
        if arguments.command == "project":
            return _run_project(arguments, config, shared_key, output)
        parser.error("unknown command")
    except _EXPECTED_ERRORS as error:
        print(f"오류: {error}", file=error_output)
        return 2
    except (OSError, sqlite3.Error):
        print("오류: 로컬 파일 또는 데이터베이스 작업에 실패했습니다.", file=error_output)
        return 2
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-usage")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--config",
        default=str(default_config_path()),
        help="local configuration JSON path",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="initialize one local device")
    initialize.add_argument("--ledger", required=True, help="private ledger checkout")
    initialize.add_argument(
        "--codex-home",
        default=str(Path.home() / ".codex"),
        help="Codex local data directory",
    )
    initialize.add_argument("--state-db", help="local SQLite state path")
    initialize.add_argument(
        "--import-key",
        action="store_true",
        help="read an existing recovery key without echo",
    )
    initialize.add_argument(
        "--force",
        action="store_true",
        help="replace an existing local configuration",
    )

    commands.add_parser("collect", help="collect and flush Codex token usage")
    commands.add_parser("sync", help="synchronize the private Git ledger")
    commands.add_parser("doctor", help="run read-only environment diagnostics")
    project = commands.add_parser("project", help="inspect and map projects")
    project_commands = project.add_subparsers(
        dest="project_command",
        required=True,
    )
    project_commands.add_parser("list", help="list known projects")
    project_commands.add_parser(
        "unresolved",
        help="list unclassified and ambiguous threads",
    )
    link = project_commands.add_parser("link", help="manually assign usage")
    link_subject = link.add_mutually_exclusive_group(required=True)
    link_subject.add_argument("--thread", help="raw local ID or thr_h1 ID")
    link_subject.add_argument("--turn", help="raw local ID or turn_h1 ID")
    link.add_argument("--project", required=True, help="target prj_h1 project ID")
    alias = project_commands.add_parser("alias", help="merge an old project ID")
    alias.add_argument("--from", dest="source_project", required=True)
    alias.add_argument("--to", dest="target_project", required=True)
    report = commands.add_parser("report", help="show project token usage")
    report.add_argument("--from", dest="from_date", help="first local date YYYY-MM-DD")
    report.add_argument("--to", dest="to_date", help="last local date YYYY-MM-DD")
    report.add_argument(
        "--period",
        choices=("all", "today", "week"),
        default="all",
    )
    report.add_argument("--timezone", default="Asia/Seoul")
    report.add_argument("--project")
    report.add_argument("--model")
    report.add_argument("--device")
    report.add_argument("--source")
    report.add_argument("--group-by", default="project,date")
    report.add_argument("--markdown", help="write the same report as Markdown")
    return parser


def _run_init(
    arguments: argparse.Namespace,
    config_path: Path,
    secrets: SecretStore,
    secret_reader: Callable[[], str],
    output: TextIO,
) -> int:
    if config_path.exists() and not arguments.force:
        raise ConfigError("configuration already exists; use --force to replace it")
    shared_key = (
        decode_recovery_key(secret_reader())
        if arguments.import_key
        else generate_shared_key()
    )
    device_id = str(uuid.uuid4())
    current_key_id = key_id(shared_key)
    credential_target = f"CodexUsageTracker/{current_key_id}"
    state_db = (
        Path(arguments.state_db).expanduser().resolve()
        if arguments.state_db
        else config_path.parent / "state.sqlite"
    )
    ledger_root = Path(arguments.ledger).expanduser().resolve()
    codex_home = Path(arguments.codex_home).expanduser().resolve()

    config = AppConfig(
        schema_version=1,
        device_id=device_id,
        codex_home=str(codex_home),
        state_db=str(state_db),
        ledger_root=str(ledger_root),
        credential_target=credential_target,
        key_id=current_key_id,
    )
    ledger_root.mkdir(parents=True, exist_ok=True)
    LocalStateStore(state_db)
    secrets.put(credential_target, shared_key)
    save_config(config, config_path, overwrite=arguments.force)

    print("초기화 완료", file=output)
    print(f"기기 ID: {device_id}", file=output)
    if not arguments.import_key:
        print("다른 기기 연결용 복구 키(안전한 곳에 1회 보관):", file=output)
        print(encode_recovery_key(shared_key), file=output)
    return 0


def _run_collect(config: AppConfig, shared_key: bytes, output: TextIO) -> int:
    result = CollectService(config, shared_key).collect()
    print(
        "수집 완료: "
        f"변경 파일 {result.changed_files}/{result.discovered_files}, "
        f"신규 이벤트 {result.new_usage_events}, "
        f"기존 이벤트 {result.existing_usage_events}",
        file=output,
    )
    print(
        f"장부 이벤트 {result.ledger_event_count}, "
        f"미분류 {result.unclassified_events}, "
        f"조회 DB generation {result.read_model_state.generation}",
        file=output,
    )
    if not result.sqlite_lineage_available:
        print("경고: Codex SQLite 계보 없이 JSONL fallback을 사용했습니다.", file=output)
    if result.busy_files:
        print(
            f"경고: 쓰기 중인 rollout {result.busy_files}개는 다음 수집에서 재시도합니다.",
            file=output,
        )
    if result.invalid_files:
        print(
            f"경고: 손상된 rollout {result.invalid_files}개는 격리하고 cursor를 유지했습니다.",
            file=output,
        )
    return 0


def _run_doctor(config: AppConfig, shared_key: bytes, output: TextIO) -> int:
    result = run_doctor(config, shared_key)
    labels = {
        CheckStatus.OK: "OK",
        CheckStatus.WARNING: "경고",
        CheckStatus.ERROR: "오류",
    }
    for check in result.checks:
        print(f"[{labels[check.status]}] {check.name}: {check.message}", file=output)
    return 2 if result.has_errors else 0


def _run_sync(config: AppConfig, shared_key: bytes, output: TextIO) -> int:
    result = SyncService(config, shared_key).sync()
    print(
        "동기화 완료: "
        f"브랜치 {result.branch}, 변경 파일 {result.changed_files}, "
        f"로컬 커밋 {'생성' if result.commit_created else '없음'}, "
        f"푸시 {'완료' if result.pushed else '불필요'}",
        file=output,
    )
    print(
        f"통합 장부 이벤트 {result.ledger_event_count}, "
        f"조회 DB generation {result.read_model_state.generation}",
        file=output,
    )
    return 0


def _run_project(
    arguments: argparse.Namespace,
    config: AppConfig,
    shared_key: bytes,
    output: TextIO,
) -> int:
    service = ProjectManagementService(config, shared_key)
    if arguments.project_command == "list":
        rows = service.list_projects()
        _print_table(
            ("project_id", "name", "tokens", "threads", "events", "excluded"),
            tuple(
                (
                    row.project_id,
                    row.name or "-",
                    f"{row.total_tokens:,}",
                    f"{row.thread_count:,}",
                    f"{row.included_events:,}",
                    f"{row.excluded_events:,}",
                )
                for row in rows
            ),
            output,
            empty_message="등록된 프로젝트가 없습니다.",
        )
        return 0
    if arguments.project_command == "unresolved":
        rows = service.list_unresolved()
        _print_table(
            ("thread_key", "local_thread_id", "reason", "tokens", "events", "excluded"),
            tuple(
                (
                    row.thread_key,
                    row.local_thread_id or "-",
                    ",".join(row.resolutions),
                    f"{row.total_tokens:,}",
                    f"{row.included_events:,}",
                    f"{row.excluded_events:,}",
                )
                for row in rows
            ),
            output,
            empty_message="미분류 또는 모호한 작업이 없습니다.",
        )
        return 0
    if arguments.project_command == "link":
        subject_type = "thread" if arguments.thread is not None else "turn"
        subject = arguments.thread if arguments.thread is not None else arguments.turn
        if not isinstance(subject, str):
            raise ProjectManagementError("manual link subject is missing")
        result = service.link(
            subject_type=subject_type,
            subject=subject,
            target_project_id=arguments.project,
        )
        _print_mapping_result("프로젝트 연결", result, output)
        return 0
    if arguments.project_command == "alias":
        result = service.alias(
            source_project_id=arguments.source_project,
            target_project_id=arguments.target_project,
        )
        _print_mapping_result("프로젝트 별칭", result, output)
        return 0
    raise ProjectManagementError("unknown project command")


def _print_mapping_result(
    label: str,
    result: MappingWriteResult,
    output: TextIO,
) -> None:
    status = "기록 완료" if result.changed else "변경 없음"
    print(
        f"{label} {status}: revision {result.revision}, "
        f"조회 DB generation {result.read_model_state.generation}",
        file=output,
    )
    if result.changed:
        print("다른 기기와 공유하려면 codex-usage sync를 실행하세요.", file=output)


def _print_table(
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    output: TextIO,
    *,
    empty_message: str,
) -> None:
    if not rows:
        print(empty_message, file=output)
        return
    widths = [_display_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _display_width(value))
    print(
        "  ".join(_pad(header, widths[index]) for index, header in enumerate(headers)),
        file=output,
    )
    print("  ".join("-" * width for width in widths), file=output)
    for row in rows:
        print(
            "  ".join(_pad(value, widths[index]) for index, value in enumerate(row)),
            file=output,
        )


def _display_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in value
    )


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - _display_width(value))


def _run_report(
    arguments: argparse.Namespace,
    config: AppConfig,
    output: TextIO,
) -> int:
    from_date, to_date = _report_dates(arguments)
    group_by = tuple(
        value.strip()
        for value in arguments.group_by.split(",")
        if value.strip()
    )
    query = ReportQuery(
        from_date=from_date,
        to_date=to_date,
        timezone_name=arguments.timezone,
        project=arguments.project,
        model=arguments.model,
        device=arguments.device,
        source=arguments.source,
        group_by=group_by,
    )
    report = build_usage_report(config.state_db, query)
    print(render_terminal(report), end="", file=output)
    if arguments.markdown:
        markdown_path = write_markdown_report(
            arguments.markdown,
            render_markdown(report),
        )
        print(f"Markdown 저장 완료: {markdown_path}", file=output)
    return 0


def _report_dates(arguments: argparse.Namespace) -> tuple[date | None, date | None]:
    try:
        explicit_from = date.fromisoformat(arguments.from_date) if arguments.from_date else None
        explicit_to = date.fromisoformat(arguments.to_date) if arguments.to_date else None
    except ValueError as error:
        raise ReportError("report date must use YYYY-MM-DD") from error
    if arguments.period != "all" and (
        explicit_from is not None or explicit_to is not None
    ):
        raise ReportError("--period today/week cannot be combined with --from/--to")
    if arguments.period == "all":
        return explicit_from, explicit_to
    try:
        today = datetime.now(ZoneInfo(arguments.timezone)).date()
    except Exception as error:
        raise ReportError("timezone is unavailable") from error
    if arguments.period == "today":
        return today, today
    return today - timedelta(days=today.weekday()), today


def _read_recovery_key() -> str:
    return getpass.getpass("복구 키: ")
