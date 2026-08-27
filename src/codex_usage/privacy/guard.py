"""Defense-in-depth privacy checks for shareable ledger events."""

from __future__ import annotations

from collections.abc import Mapping
import re
import uuid


class PrivacyViolation(ValueError):
    """A sanitized event still contains a forbidden field or raw identifier."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


_FORBIDDEN_KEYS = {
    "prompt",
    "prompts",
    "response",
    "responses",
    "message",
    "messages",
    "conversation",
    "code",
    "command",
    "command_output",
    "stdout",
    "stderr",
    "cwd",
    "workdir",
    "path",
    "source_path",
    "remote",
    "remote_url",
    "repository_url",
    "git_origin_url",
    "git_branch",
    "git_sha",
    "commit_hash",
}
_NETWORK_URL = re.compile(r"(?i)\b(?:https?|ssh|git|file)://")
_SCP_REMOTE = re.compile(r"(?i)(?:^|\s)[^@\s]+@[^:\s]+:[^\s]+")
_HOST_PATH = re.compile(r"(?i)\b[a-z0-9.-]+\.[a-z]{2,}(?::\d+)?/[^\s]+")
_WINDOWS_PATH = re.compile(r"(?i)(?:^|[\s'\"(=])(?:[a-z]:[\\/]|\\\\)")
_POSIX_PATH = re.compile(r"(?:^|[\s'\"(=])(?:/[^\s]+|~[/\\]|\.\.?[/\\])")
_OPAQUE_SUFFIX = re.compile(r"[A-Za-z0-9_-]+")


class LedgerPrivacyGuard:
    """Verify that ledger IDs are pseudonymous and values contain no raw paths."""

    def validate(self, event: Mapping[str, object]) -> None:
        if not isinstance(event, Mapping):
            raise PrivacyViolation("$", "ledger event must be an object")
        self._scan(event, "$")

        event_type = event.get("event_type")
        _require_prefix(event, "key_id", "key_h1_", "$")
        if event_type == "usage_checkpoint":
            self._validate_usage(event)
        elif event_type == "mapping":
            self._validate_mapping(event)
        elif event_type == "quota_snapshot":
            _require_prefix(event, "event_id", "quota_h1_", "$")
            _optional_prefix(event, "scope_key", "account_h1_", "$")
        else:
            raise PrivacyViolation("$.event_type", "unsupported event type")

    def _validate_usage(self, event: Mapping[str, object]) -> None:
        _require_prefix(event, "event_id", "evt_h1_", "$")
        _require_prefix(event, "source_event_id", "src_h1_", "$")
        _optional_prefix(event, "supersedes", "evt_h1_", "$")
        _optional_prefix(event, "project_id", "prj_h1_", "$")
        _require_prefix(event, "thread_key", "thr_h1_", "$")
        for field in (
            "root_thread_key",
            "parent_thread_key",
            "forked_from_thread_key",
        ):
            _optional_prefix(event, field, "thr_h1_", "$")
        _optional_prefix(event, "turn_key", "turn_h1_", "$")

    def _validate_mapping(self, event: Mapping[str, object]) -> None:
        _require_prefix(event, "event_id", "map_h1_", "$")
        _optional_prefix(event, "supersedes", "map_h1_", "$")
        _optional_prefix(event, "target_project_id", "prj_h1_", "$")

        subject_type = event.get("subject_type")
        subject_prefixes = {
            "project": "prj_h1_",
            "thread": "thr_h1_",
            "turn": "turn_h1_",
            "local_repo": "local_h1_",
        }
        if subject_type == "device":
            subject_id = event.get("subject_id")
            if not isinstance(subject_id, str) or not subject_id:
                raise PrivacyViolation("$.subject_id", "device ID is missing")
            try:
                canonical = str(uuid.UUID(subject_id))
            except (ValueError, AttributeError) as error:
                raise PrivacyViolation(
                    "$.subject_id",
                    "device ID is not a UUID",
                ) from error
            if subject_id != canonical:
                raise PrivacyViolation(
                    "$.subject_id",
                    "device ID is not canonical",
                )
            return
        prefix = subject_prefixes.get(subject_type)
        if prefix is None:
            raise PrivacyViolation("$.subject_type", "unsupported mapping subject")
        _require_prefix(event, "subject_id", prefix, "$")

    def _scan(self, value: object, path: str) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise PrivacyViolation(path, "object key is not a string")
                child_path = f"{path}.{key}"
                if key.lower() in _FORBIDDEN_KEYS:
                    raise PrivacyViolation(child_path, "forbidden raw field")
                self._scan(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._scan(child, f"{path}[{index}]")
        elif isinstance(value, str):
            if _NETWORK_URL.search(value) or _SCP_REMOTE.search(value):
                raise PrivacyViolation(path, "raw network location detected")
            if _HOST_PATH.search(value):
                raise PrivacyViolation(path, "raw host/path value detected")
            if _WINDOWS_PATH.search(value) or _POSIX_PATH.search(value):
                raise PrivacyViolation(path, "raw filesystem path detected")


def _require_prefix(
    event: Mapping[str, object],
    field: str,
    prefix: str,
    path: str,
) -> None:
    value = event.get(field)
    if not _is_pseudonymous_id(value, prefix):
        raise PrivacyViolation(f"{path}.{field}", "identifier is not pseudonymous")


def _optional_prefix(
    event: Mapping[str, object],
    field: str,
    prefix: str,
    path: str,
) -> None:
    value = event.get(field)
    if value is None:
        return
    if not _is_pseudonymous_id(value, prefix):
        raise PrivacyViolation(f"{path}.{field}", "identifier is not pseudonymous")


def _is_pseudonymous_id(value: object, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    suffix = value[len(prefix) :]
    expected_length = 22 if prefix == "key_h1_" else 43
    return len(suffix) == expected_length and _OPAQUE_SUFFIX.fullmatch(suffix) is not None
