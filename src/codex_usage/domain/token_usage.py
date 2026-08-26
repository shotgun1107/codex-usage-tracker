"""Token checkpoint domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Mapping


TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class TokenDataError(ValueError):
    """Raised when token data is malformed."""


class Operation(StrEnum):
    """The lifecycle operation that produced a token checkpoint."""

    TURN = "turn"
    COMPACT = "compact"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Codex token counters. ``None`` means the source field was unavailable."""

    field_names: ClassVar[tuple[str, ...]] = TOKEN_FIELDS

    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None

    def __post_init__(self) -> None:
        for field_name in self.field_names:
            value = getattr(self, field_name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TokenDataError(
                    f"{field_name} must be a non-negative integer or None"
                )

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> TokenCounts:
        """Build counts from a Codex token-usage mapping."""

        if not isinstance(values, Mapping):
            raise TokenDataError("token counts must be a mapping")
        return cls(**{field_name: values.get(field_name) for field_name in cls.field_names})

    def is_total_consistent(self) -> bool | None:
        """Return whether input + output equals total, or None if unknown."""

        if (
            self.input_tokens is None
            or self.output_tokens is None
            or self.total_tokens is None
        ):
            return None
        return self.input_tokens + self.output_tokens == self.total_tokens


@dataclass(frozen=True, slots=True)
class RawTokenCheckpoint:
    """One token checkpoint parsed from a rollout."""

    rollout_thread_id: str
    rollout_forked_from_id: str | None
    turn_id: str | None
    token_event_ordinal: int
    record_index: int
    occurred_at: datetime
    operation: Operation
    model: str | None
    reasoning_effort: str | None
    cumulative: TokenCounts
    reported_last: TokenCounts | None

    def __post_init__(self) -> None:
        if not self.rollout_thread_id:
            raise TokenDataError("rollout_thread_id must not be empty")
        if self.token_event_ordinal < 0:
            raise TokenDataError("token_event_ordinal must not be negative")
        if self.record_index < 1:
            raise TokenDataError("record_index must be one-based")
        if self.occurred_at.tzinfo is None:
            raise TokenDataError("occurred_at must be timezone-aware")
        if self.cumulative.total_tokens is None:
            raise TokenDataError("cumulative total_tokens is required")

    @property
    def logical_key(self) -> tuple[str, str, int]:
        """Return the raw logical key used before HMAC encoding."""

        if self.turn_id is not None:
            return ("turn", self.turn_id, self.token_event_ordinal)
        return ("record", self.rollout_thread_id, self.record_index)


@dataclass(frozen=True, slots=True)
class CalculatedTokenEvent:
    """A checkpoint paired with its calculated usage delta."""

    checkpoint: RawTokenCheckpoint
    delta: TokenCounts | None
    flags: tuple[str, ...] = ()

    @property
    def logical_key(self) -> tuple[str, str, int]:
        return self.checkpoint.logical_key
