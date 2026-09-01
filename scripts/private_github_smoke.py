"""Synthetic Codex Usage Tracker smoke test against a private GitHub remote."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_usage.application.doctor import CheckStatus, run_doctor
from codex_usage.application.sync import SyncService
from codex_usage.config import AppConfig
from codex_usage.ledger.jsonl import LedgerWriter
from codex_usage.privacy.identifiers import (
    generate_shared_key,
    key_id,
    project_id,
    source_event_id,
    thread_key,
    turn_key,
    usage_event_id,
)
from codex_usage.reports.query import ReportQuery, build_usage_report
from codex_usage.storage.sqlite import LocalStateStore


CODE_REPOSITORY = "shotgun1107/codex-usage-tracker"
SYNTHETIC_TOTAL = 123


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--remote",
        required=True,
        help="HTTPS URL of an existing initialized private GitHub repository",
    )
    arguments = parser.parse_args(argv)
    repository = github_repository_name(arguments.remote)
    if repository.casefold() == CODE_REPOSITORY.casefold():
        raise RuntimeError("refusing to use the public source repository as a ledger")
    verify_private_repository(repository)

    branch = f"codex-usage-smoke-{uuid.uuid4().hex[:12]}"
    cleanup_error: Exception | None = None
    succeeded = False
    with tempfile.TemporaryDirectory(prefix="codex-usage-private-smoke-") as directory:
        root = Path(directory)
        first_checkout = root / "device-one" / "ledger"
        first_checkout.parent.mkdir()
        branch_pushed = False
        try:
            run_git(None, "clone", arguments.remote, str(first_checkout))
            configure_identity(first_checkout)
            run_git(first_checkout, "switch", "-c", branch)
            run_git(first_checkout, "push", "--set-upstream", "origin", f"HEAD:{branch}")
            branch_pushed = True

            shared_key = generate_shared_key()
            first = make_config(
                root / "device-one",
                first_checkout,
                shared_key,
            )
            event = synthetic_event(shared_key, first.device_id)
            store = LocalStateStore(first.state_db)
            store.enqueue_outbox_events((event,))
            LedgerWriter(
                first.ledger_root,
                first.device_id,
                expected_key_id=first.key_id,
            ).flush(store, limit=100)
            SyncService(first, shared_key).sync()

            second_checkout = root / "device-two" / "ledger"
            second_checkout.parent.mkdir()
            run_git(
                None,
                "clone",
                "--branch",
                branch,
                arguments.remote,
                str(second_checkout),
            )
            configure_identity(second_checkout)
            second = make_config(
                root / "device-two",
                second_checkout,
                shared_key,
            )
            SyncService(second, shared_key).sync()

            report = build_usage_report(
                second.state_db,
                ReportQuery(group_by=("project",)),
            )
            if report.total.total_tokens.value != SYNTHETIC_TOTAL:
                raise RuntimeError("clean clone report total did not match synthetic input")
            doctor = {
                check.name: check
                for check in run_doctor(second, shared_key).checks
            }
            for name in ("ledger-remote", "read-model", "classification"):
                if doctor[name].status is not CheckStatus.OK:
                    raise RuntimeError(f"doctor check did not pass: {name}")
            succeeded = True
        finally:
            if branch_pushed:
                try:
                    run_git(first_checkout, "push", "origin", "--delete", branch)
                except Exception as error:
                    cleanup_error = error
                    print(
                        f"WARNING: delete remote branch {branch} manually",
                        file=sys.stderr,
                    )

    if cleanup_error is not None:
        raise RuntimeError(
            f"smoke branch cleanup failed; delete branch {branch} manually"
        ) from cleanup_error
    if not succeeded:
        raise RuntimeError("private GitHub smoke did not complete")
    print(
        f"Private GitHub smoke passed for {repository}; "
        f"synthetic total {SYNTHETIC_TOTAL}; temporary branch deleted."
    )
    return 0


def github_repository_name(remote: str) -> str:
    parsed = urlsplit(remote)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("remote must be an HTTPS github.com repository URL")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("GitHub remote must contain exactly owner/repository")
    repository = parts[1][:-4] if parts[1].lower().endswith(".git") else parts[1]
    if not parts[0] or not repository:
        raise ValueError("GitHub owner and repository must not be empty")
    return f"{parts[0]}/{repository}"


def verify_private_repository(repository: str) -> None:
    result = run(
        "gh",
        "repo",
        "view",
        repository,
        "--json",
        "isPrivate,nameWithOwner",
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("GitHub repository visibility could not be verified") from error
    if value.get("isPrivate") is not True:
        raise RuntimeError("refusing to run against a non-private repository")
    if str(value.get("nameWithOwner", "")).casefold() != repository.casefold():
        raise RuntimeError("GitHub repository identity did not match the requested remote")


def make_config(
    device_root: Path,
    ledger: Path,
    shared_key: bytes,
) -> AppConfig:
    device_root.mkdir(exist_ok=True)
    codex_home = device_root / "codex-home"
    codex_home.mkdir()
    current_key_id = key_id(shared_key)
    return AppConfig(
        schema_version=1,
        device_id=str(uuid.uuid4()),
        codex_home=str(codex_home.resolve()),
        state_db=str((device_root / "state.sqlite").resolve()),
        ledger_root=str(ledger.resolve()),
        credential_target=f"CodexUsageTracker/{current_key_id}",
        key_id=current_key_id,
    )


def synthetic_event(shared_key: bytes, device_id: str) -> dict[str, object]:
    raw_thread = "private-github-smoke-thread"
    raw_turn = "private-github-smoke-turn"
    logical_source = source_event_id(shared_key, raw_turn, 0)
    counts = {
        "input_tokens": 113,
        "cached_input_tokens": 7,
        "cache_write_input_tokens": None,
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
        "total_tokens": SYNTHETIC_TOTAL,
    }
    payload: dict[str, object] = {
        "schema_version": 1,
        "event_type": "usage_checkpoint",
        "source_event_id": logical_source,
        "revision": 1,
        "supersedes": None,
        "voided": False,
        "parser_version": "private-github-smoke",
        "device_id": device_id,
        "key_id": key_id(shared_key),
        "project_id": project_id(
            shared_key,
            "github.com/synthetic/private-smoke",
        ),
        "project_resolution": "manual",
        "activity_repository_count": 0,
        "thread_key": thread_key(shared_key, raw_thread),
        "root_thread_key": thread_key(shared_key, raw_thread),
        "parent_thread_key": None,
        "forked_from_thread_key": None,
        "turn_key": turn_key(shared_key, raw_turn),
        "token_event_ordinal": 0,
        "operation": "turn",
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "model": "synthetic-smoke-model",
        "reasoning_effort": "medium",
        "source_kind": "cli",
        "cli_version": "private-github-smoke",
        "cumulative": counts,
        "delta": counts,
        "reported_last": counts,
        "flags": [],
    }
    payload["event_id"] = usage_event_id(
        shared_key,
        logical_source,
        1,
        payload,
    )
    return payload


def configure_identity(checkout: Path) -> None:
    run_git(checkout, "config", "user.name", "Codex Usage Smoke")
    run_git(checkout, "config", "user.email", "codex-usage@example.invalid")


def run_git(
    checkout: Path | None,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if checkout is not None:
        command.extend(("-C", str(checkout)))
    command.extend(arguments)
    return run(*command)


def run(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        arguments,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
    )


if __name__ == "__main__":
    raise SystemExit(main())
