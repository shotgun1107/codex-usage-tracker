"""User-facing init, collect, and doctor commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
import getpass
from pathlib import Path
import sqlite3
import sys
from typing import TextIO
import uuid
from zoneinfo import ZoneInfo

from codex_usage import __version__
from codex_usage.application.collect import CollectError, CollectService
from codex_usage.application.doctor import CheckStatus, run_doctor
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


_EXPECTED_ERRORS = (
    CollectError,
    ConfigError,
    LedgerIoError,
    LedgerReplayError,
    LedgerSchemaError,
    LocalStoreError,
    PrivacyViolation,
    ReportError,
    SecretStoreError,
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
        if arguments.command == "doctor":
            return _run_doctor(config, shared_key, output)
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
    commands.add_parser("doctor", help="run read-only environment diagnostics")
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
