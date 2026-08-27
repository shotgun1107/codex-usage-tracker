"""Validated append-only JSONL ledger reader and outbox writer."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Protocol
import uuid

from codex_usage.ledger.schema_validation import LedgerSchemaValidator
from codex_usage.privacy.guard import LedgerPrivacyGuard


class LedgerIoError(RuntimeError):
    """A ledger file is unsafe, malformed, or inconsistent."""


class LedgerEventConflict(LedgerIoError):
    """One event ID already exists with different serialized content."""


@dataclass(frozen=True, slots=True)
class LedgerReadIssue:
    code: str
    relative_path: str


@dataclass(frozen=True, slots=True)
class LedgerReadResult:
    events: tuple[Mapping[str, object], ...]
    issues: tuple[LedgerReadIssue, ...]


@dataclass(frozen=True, slots=True)
class LedgerFlushResult:
    pending_seen: int
    appended: int
    already_present: int
    partial_tails_recovered: int


class PendingEvent(Protocol):
    event_id: str

    def payload(self) -> dict[str, object]: ...


class OutboxStore(Protocol):
    def pending_outbox(self, *, limit: int = 1_000) -> tuple[PendingEvent, ...]: ...

    def mark_outbox_flushed(self, ledger_paths: Mapping[str, str]) -> None: ...


class LedgerReader:
    """Read every complete validated ledger line without trusting file order."""

    def __init__(
        self,
        ledger_root: str | Path,
        *,
        validator: LedgerSchemaValidator | None = None,
        privacy_guard: LedgerPrivacyGuard | None = None,
    ) -> None:
        self.root = Path(ledger_root).expanduser().resolve()
        self.validator = validator or LedgerSchemaValidator.default()
        self.privacy_guard = privacy_guard or LedgerPrivacyGuard()

    def read_all(self) -> LedgerReadResult:
        if not self.root.exists():
            return LedgerReadResult((), ())
        if not self.root.is_dir() or self.root.is_symlink():
            raise LedgerIoError("ledger root must be a real directory")

        events: list[Mapping[str, object]] = []
        issues: list[LedgerReadIssue] = []
        for path in sorted(self.root.rglob("*.jsonl")):
            relative = _safe_existing_relative(self.root, path)
            complete_lines, has_partial = _complete_lines(path)
            if has_partial:
                issues.append(
                    LedgerReadIssue("partial_final_line_ignored", relative.as_posix())
                )
            for line_number, line in enumerate(complete_lines, start=1):
                event = _decode_event(line, relative, line_number)
                self.validator.validate(event)
                self.privacy_guard.validate(event)
                expected = ledger_relative_path(event)
                if expected != relative:
                    raise LedgerIoError(
                        "ledger event is stored under an unexpected relative path"
                    )
                events.append(MappingProxyType(event))
        return LedgerReadResult(tuple(events), tuple(issues))


class LedgerWriter:
    """Flush sanitized outbox rows to one device's append-only ledger area."""

    def __init__(
        self,
        ledger_root: str | Path,
        device_id: str,
        *,
        validator: LedgerSchemaValidator | None = None,
        privacy_guard: LedgerPrivacyGuard | None = None,
    ) -> None:
        self.root = Path(ledger_root).expanduser().resolve()
        self.device_id = _canonical_device_id(device_id)
        self.validator = validator or LedgerSchemaValidator.default()
        self.privacy_guard = privacy_guard or LedgerPrivacyGuard()

    def flush(self, store: OutboxStore, *, limit: int = 1_000) -> LedgerFlushResult:
        pending = store.pending_outbox(limit=limit)
        grouped: dict[PurePosixPath, list[tuple[PendingEvent, str]]] = defaultdict(list)

        for item in pending:
            event = item.payload()
            if event.get("event_id") != item.event_id:
                raise LedgerEventConflict(
                    "outbox identity does not match its serialized event"
                )
            self.validator.validate(event)
            self.privacy_guard.validate(event)
            if event.get("device_id") != self.device_id:
                raise LedgerIoError("outbox event belongs to another device")
            relative = ledger_relative_path(event, expected_device_id=self.device_id)
            grouped[relative].append((item, _canonical_json(event)))

        appended = 0
        already_present = 0
        recovered = 0
        for relative in sorted(grouped, key=lambda value: value.as_posix()):
            target = _safe_write_target(self.root, relative)
            existing, had_partial = self._existing_events(target, relative)
            if had_partial:
                _truncate_partial_tail(target)
                recovered += 1

            missing: list[tuple[PendingEvent, str]] = []
            for item, canonical in grouped[relative]:
                previous = existing.get(item.event_id)
                if previous is None:
                    missing.append((item, canonical))
                elif previous == canonical:
                    already_present += 1
                else:
                    raise LedgerEventConflict(
                        "event_id exists with different ledger content"
                    )

            if missing:
                with target.open("ab", buffering=0) as output:
                    for _, canonical in missing:
                        encoded = canonical.encode("utf-8") + b"\n"
                        if output.write(encoded) != len(encoded):
                            raise LedgerIoError("ledger line was only partially written")
                    os.fsync(output.fileno())
                appended += len(missing)

            store.mark_outbox_flushed(
                {item.event_id: relative.as_posix() for item, _ in grouped[relative]}
            )

        return LedgerFlushResult(
            pending_seen=len(pending),
            appended=appended,
            already_present=already_present,
            partial_tails_recovered=recovered,
        )

    def _existing_events(
        self,
        target: Path,
        relative: PurePosixPath,
    ) -> tuple[dict[str, str], bool]:
        if not target.exists():
            return {}, False
        complete_lines, has_partial = _complete_lines(target)
        events: dict[str, str] = {}
        for line_number, line in enumerate(complete_lines, start=1):
            event = _decode_event(line, relative, line_number)
            self.validator.validate(event)
            self.privacy_guard.validate(event)
            if ledger_relative_path(event) != relative:
                raise LedgerIoError(
                    "ledger event is stored under an unexpected relative path"
                )
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise LedgerIoError("ledger event_id is missing")
            canonical = _canonical_json(event)
            previous = events.get(event_id)
            if previous is not None and previous != canonical:
                raise LedgerEventConflict(
                    "ledger file contains conflicting duplicate event IDs"
                )
            events[event_id] = canonical
        return events, has_partial


def ledger_relative_path(
    event: Mapping[str, object],
    *,
    expected_device_id: str | None = None,
) -> PurePosixPath:
    device_id = event.get("device_id")
    if not isinstance(device_id, str):
        raise LedgerIoError("ledger device_id is missing")
    canonical_device = _canonical_device_id(device_id)
    if device_id != canonical_device:
        raise LedgerIoError("ledger device_id must use canonical UUID text")
    if expected_device_id is not None and canonical_device != expected_device_id:
        raise LedgerIoError("ledger event belongs to another device")

    occurred_at = event.get("occurred_at")
    if not isinstance(occurred_at, str):
        raise LedgerIoError("ledger occurred_at is missing")
    timestamp = _parse_utc(occurred_at)
    year = f"{timestamp.year:04d}"
    month = f"{timestamp.month:02d}"
    day = f"{timestamp.day:02d}.jsonl"

    event_type = event.get("event_type")
    base = PurePosixPath("devices") / canonical_device
    if event_type == "usage_checkpoint":
        return base / "usage" / year / month / day
    if event_type == "mapping":
        return base / "mappings" / year / f"{month}.jsonl"
    if event_type == "quota_snapshot":
        return base / "quota" / year / month / day
    raise LedgerIoError("unsupported ledger event type")


def _safe_existing_relative(root: Path, path: Path) -> PurePosixPath:
    if path.is_symlink() or _has_symlink_between(root, path):
        raise LedgerIoError("ledger files and parents must not be symlinks")
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as error:
        raise LedgerIoError("ledger file escapes the configured root") from error
    if not resolved.is_file():
        raise LedgerIoError("ledger JSONL path is not a regular file")
    return PurePosixPath(relative.as_posix())


def _safe_write_target(root: Path, relative: PurePosixPath) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise LedgerIoError("ledger root must be a real directory")

    current = root
    for component in relative.parts[:-1]:
        current = current / component
        if current.exists() and current.is_symlink():
            raise LedgerIoError("ledger parent directory must not be a symlink")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise LedgerIoError("ledger parent path is not a directory")

    target = current / relative.name
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise LedgerIoError("ledger target must be a regular file")
    try:
        target.resolve().relative_to(root)
    except ValueError as error:
        raise LedgerIoError("ledger target escapes the configured root") from error
    return target


def _has_symlink_between(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            return True
    return False


def _complete_lines(path: Path) -> tuple[tuple[bytes, ...], bool]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise LedgerIoError("ledger file cannot be read") from error
    if not data:
        return (), False
    has_partial = not data.endswith(b"\n")
    complete = data if not has_partial else data[: data.rfind(b"\n") + 1]
    return tuple(complete.splitlines()), has_partial


def _truncate_partial_tail(path: Path) -> None:
    try:
        with path.open("r+b", buffering=0) as output:
            data = output.read()
            last_newline = data.rfind(b"\n")
            output.seek(0)
            output.truncate(last_newline + 1)
            os.fsync(output.fileno())
    except OSError as error:
        raise LedgerIoError("partial ledger tail cannot be recovered") from error


def _decode_event(
    line: bytes,
    relative: PurePosixPath,
    line_number: int,
) -> dict[str, object]:
    if not line:
        raise LedgerIoError("ledger contains a blank complete line")
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LedgerIoError(
            f"ledger contains invalid JSON at line {line_number}"
        ) from error
    if not isinstance(value, dict):
        raise LedgerIoError("ledger line must contain a JSON object")
    return value


def _canonical_json(event: Mapping[str, object]) -> str:
    try:
        return json.dumps(
            dict(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise LedgerIoError("ledger event cannot be serialized") from error


def _canonical_device_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise LedgerIoError("device_id is not a UUID") from error
    canonical = str(parsed)
    if value != canonical:
        raise LedgerIoError("device_id must use canonical UUID text")
    return canonical


def _parse_utc(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise LedgerIoError("occurred_at is not an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise LedgerIoError("occurred_at must use UTC")
    return parsed
