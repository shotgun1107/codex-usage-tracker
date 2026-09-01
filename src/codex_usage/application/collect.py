"""End-to-end collection from local Codex rollouts into the Git ledger."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from codex_usage import __version__
from codex_usage.application.project_attribution import ProjectAttributionEngine
from codex_usage.application.lock import ApplicationLock
from codex_usage.config import AppConfig
from codex_usage.domain.lifecycle import (
    DuplicateCheckpointConflict,
    calculate_deltas,
    deduplicate_events,
)
from codex_usage.ledger.jsonl import LedgerFlushResult, LedgerReader, LedgerWriter
from codex_usage.ledger.replay import replay_ledger_events
from codex_usage.privacy.encoder import UsageEventEncoder
from codex_usage.privacy.identifiers import key_id
from codex_usage.sources.codex_jsonl import (
    RolloutParseError,
    RolloutParseResult,
    parse_rollout,
)
from codex_usage.sources.codex_sqlite import (
    InventoryIssue,
    SqliteAdapterError,
    ThreadInventory,
    load_thread_inventory,
)
from codex_usage.sources.rollout_files import (
    PreviousRolloutCursor,
    RolloutFileError,
    RolloutFileBusy,
    RolloutFileSnapshot,
    discover_rollout_files,
    read_changed_rollout,
    rollout_source_id,
)
from codex_usage.storage.sqlite import (
    LocalStateStore,
    ParserIssueRecord,
    ReadModelState,
    SourceCursor,
)


class CollectError(RuntimeError):
    """Collection cannot continue without risking incorrect usage data."""


@dataclass(frozen=True, slots=True)
class CollectResult:
    discovered_files: int
    changed_files: int
    busy_files: int
    invalid_files: int
    parsed_files: int
    calculated_checkpoints: int
    new_usage_events: int
    existing_usage_events: int
    parser_issue_count: int
    unclassified_events: int
    sqlite_lineage_available: bool
    flushed: LedgerFlushResult
    ledger_event_count: int
    ledger_partial_line_issues: int
    read_model_state: ReadModelState


class CollectService:
    """Coordinate adapters while keeping raw paths and IDs on the device."""

    def __init__(
        self,
        config: AppConfig,
        shared_key: bytes,
        *,
        parser_version: str = __version__,
    ) -> None:
        if key_id(shared_key) != config.key_id:
            raise CollectError("configured HMAC key does not match key_id")
        self.config = config
        self.shared_key = shared_key
        self.encoder = UsageEventEncoder(
            shared_key,
            config.device_id,
            parser_version=parser_version,
        )

    def collect(self) -> CollectResult:
        with ApplicationLock(self.config.state_db):
            store = LocalStateStore(self.config.state_db)
            run_id = store.begin_collect_run()
            try:
                result = self._collect(store)
            except Exception as error:
                store.finish_collect_run(
                    run_id,
                    "failed",
                    type(error).__name__,
                )
                raise
            store.finish_collect_run(run_id, "succeeded")
            return result

    def _collect(self, store: LocalStateStore) -> CollectResult:
        reader = LedgerReader(self.config.ledger_root)
        ledger_before = reader.read_all()
        ledger_key_ids = {
            event.get("key_id")
            for event in ledger_before.events
            if isinstance(event.get("key_id"), str)
        }
        if ledger_key_ids - {self.config.key_id}:
            raise CollectError("ledger contains a different HMAC key_id")
        known_source_ids = set(store.known_usage_source_event_ids())
        known_source_ids.update(
            event["source_event_id"]
            for event in ledger_before.events
            if event.get("event_type") == "usage_checkpoint"
            and isinstance(event.get("source_event_id"), str)
        )

        inventory, sqlite_available = _load_inventory(Path(self.config.codex_home))
        paths = discover_rollout_files(self.config.codex_home)
        cursors = store.all_cursors()
        snapshots: list[RolloutFileSnapshot] = []
        busy_files = 0
        for path in paths:
            source_id = rollout_source_id(path)
            cursor = cursors.get(source_id)
            previous = (
                PreviousRolloutCursor(cursor.fingerprint, cursor.byte_offset)
                if cursor is not None
                else None
            )
            try:
                snapshot = read_changed_rollout(path, previous)
            except RolloutFileBusy:
                busy_files += 1
                continue
            except RolloutFileError as error:
                raise CollectError("a rollout file could not be snapshotted") from error
            if snapshot is not None:
                snapshots.append(snapshot)

        parsed_by_source: dict[str, RolloutParseResult] = {}
        metadata_by_thread = {}
        calculated = []
        source_by_thread: dict[str, str] = {}
        parser_issue_count = 0
        invalid_source_ids: set[str] = set()
        for snapshot in snapshots:
            if not snapshot.lines:
                continue
            try:
                parsed = parse_rollout(snapshot.lines)
            except RolloutParseError as error:
                invalid_source_ids.add(snapshot.source_id)
                parser_issue_count += 1
                store.record_parser_issues(
                    (
                        ParserIssueRecord(
                            snapshot.source_id,
                            "fatal_rollout_parse_error",
                            error.record_index,
                        ),
                    )
                )
                continue
            parsed_by_source[snapshot.source_id] = parsed
            metadata_by_thread.setdefault(parsed.metadata.thread_id, parsed.metadata)
            source_by_thread.setdefault(parsed.metadata.thread_id, snapshot.source_id)
            parser_issue_count += len(parsed.issues)
            calculated.extend(calculate_deltas(parsed.checkpoints))

        try:
            logical_events = deduplicate_events(calculated)
        except DuplicateCheckpointConflict as error:
            raise CollectError("logical token checkpoints conflict") from error

        events_by_source: dict[str, list[dict[str, object]]] = defaultdict(list)
        existing_usage_events = 0
        unclassified_events = 0
        if logical_events:
            attribution_engine = ProjectAttributionEngine(
                metadata_by_thread,
                inventory,
            )
            attributed = attribution_engine.attribute_all(
                event.checkpoint for event in logical_events
            )
            for calculated_event, attributed_checkpoint in zip(
                logical_events,
                attributed,
                strict=True,
            ):
                logical_source_id = self.encoder.source_id(calculated_event)
                if logical_source_id in known_source_ids:
                    existing_usage_events += 1
                    continue
                checkpoint = calculated_event.checkpoint
                metadata = metadata_by_thread.get(checkpoint.rollout_thread_id)
                source_id = source_by_thread.get(checkpoint.rollout_thread_id)
                if metadata is None or source_id is None:
                    raise CollectError("checkpoint source metadata is unavailable")
                encoded = self.encoder.encode(
                    calculated_event,
                    attributed_checkpoint.attribution,
                    metadata,
                    attribution_engine.lineage,
                )
                events_by_source[source_id].append(encoded)
                known_source_ids.add(logical_source_id)
                if encoded["project_id"] is None:
                    unclassified_events += 1

        new_usage_events = 0
        for snapshot in snapshots:
            if snapshot.source_id in invalid_source_ids:
                continue
            parsed = parsed_by_source.get(snapshot.source_id)
            issues = (
                tuple(
                    ParserIssueRecord(
                        snapshot.source_id,
                        issue.code,
                        issue.record_index,
                        parsed.metadata.cli_version,
                    )
                    for issue in parsed.issues
                )
                if parsed is not None
                else ()
            )
            new_usage_events += store.store_collection(
                SourceCursor(
                    source_id=snapshot.source_id,
                    source_path=snapshot.source_path,
                    fingerprint=snapshot.fingerprint,
                    byte_offset=snapshot.byte_offset,
                    last_complete_line_digest=snapshot.last_complete_line_digest,
                ),
                events_by_source.get(snapshot.source_id, ()),
                parser_issues=issues,
            )

        pending_before_flush = store.pending_outbox(limit=1_000_000)
        flushed = _flush_all(
            LedgerWriter(
                self.config.ledger_root,
                self.config.device_id,
                expected_key_id=self.config.key_id,
            ),
            store,
        )
        if len(pending_before_flush) != flushed.pending_seen:
            raise CollectError("pending outbox exceeded one collect transaction")
        combined_events = list(ledger_before.events)
        combined_event_ids = {
            event.get("event_id")
            for event in combined_events
            if isinstance(event.get("event_id"), str)
        }
        for pending in pending_before_flush:
            payload = pending.payload()
            if pending.event_id not in combined_event_ids:
                combined_events.append(payload)
                combined_event_ids.add(pending.event_id)
        replay = replay_ledger_events(
            combined_events,
            expected_key_id=self.config.key_id,
        )
        read_model_state = store.rebuild_read_model(replay)

        return CollectResult(
            discovered_files=len(paths),
            changed_files=len(snapshots),
            busy_files=busy_files,
            invalid_files=len(invalid_source_ids),
            parsed_files=len(parsed_by_source),
            calculated_checkpoints=len(logical_events),
            new_usage_events=new_usage_events,
            existing_usage_events=existing_usage_events,
            parser_issue_count=parser_issue_count,
            unclassified_events=unclassified_events,
            sqlite_lineage_available=sqlite_available,
            flushed=flushed,
            ledger_event_count=len(combined_events),
            ledger_partial_line_issues=max(
                0,
                len(ledger_before.issues) - flushed.partial_tails_recovered,
            ),
            read_model_state=read_model_state,
        )


def find_codex_state_database(codex_home: str | Path) -> Path | None:
    root = Path(codex_home).expanduser().resolve()
    candidates = tuple(root.glob("state_*.sqlite"))
    if not candidates:
        return None

    def rank(path: Path) -> tuple[int, int]:
        suffix = path.stem.removeprefix("state_")
        version = int(suffix) if suffix.isdigit() else -1
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            modified = -1
        return version, modified

    return max(candidates, key=rank)


def _load_inventory(codex_home: Path) -> tuple[ThreadInventory, bool]:
    database = find_codex_state_database(codex_home)
    if database is None:
        return _empty_inventory("codex_state_database_missing"), False
    try:
        return load_thread_inventory(database), True
    except SqliteAdapterError:
        return _empty_inventory("codex_state_database_unavailable"), False


def _empty_inventory(issue_code: str) -> ThreadInventory:
    return ThreadInventory(
        threads=MappingProxyType({}),
        spawn_edges=(),
        issues=(InventoryIssue(issue_code),),
    )


def _flush_all(writer: LedgerWriter, store: LocalStateStore) -> LedgerFlushResult:
    totals = Counter()
    while True:
        result = writer.flush(store, limit=100_000)
        if result.pending_seen == 0:
            break
        if result.appended + result.already_present != result.pending_seen:
            raise CollectError("ledger writer made no complete outbox progress")
        totals["pending_seen"] += result.pending_seen
        totals["appended"] += result.appended
        totals["already_present"] += result.already_present
        totals["partial_tails_recovered"] += result.partial_tails_recovered
    return LedgerFlushResult(
        pending_seen=totals["pending_seen"],
        appended=totals["appended"],
        already_present=totals["already_present"],
        partial_tails_recovered=totals["partial_tails_recovered"],
    )
