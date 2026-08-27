"""Dependency-free validator for the project's JSON Schema v1 subset."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import math
from pathlib import Path
import sysconfig
import uuid


class LedgerSchemaError(ValueError):
    """A ledger value does not satisfy the checked-in JSON Schema."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class LedgerSchemaValidator:
    """Validate the exact JSON Schema features used by ledger event v1."""

    def __init__(self, schema: Mapping[str, object]) -> None:
        self._schema = _json_object_copy(schema, "schema")

    @classmethod
    def from_file(cls, path: str | Path) -> LedgerSchemaValidator:
        schema_path = Path(path)
        try:
            value = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LedgerSchemaError("$", "schema file cannot be read") from error
        if not isinstance(value, Mapping):
            raise LedgerSchemaError("$", "schema root must be an object")
        return cls(value)

    @classmethod
    def default(cls) -> LedgerSchemaValidator:
        project_root = Path(__file__).resolve().parents[3]
        candidates = (
            project_root / "schemas" / "ledger-event-v1.schema.json",
            Path(sysconfig.get_path("data"))
            / "share"
            / "codex-usage-tracker"
            / "schemas"
            / "ledger-event-v1.schema.json",
        )
        for candidate in candidates:
            if candidate.is_file():
                return cls.from_file(candidate)
        raise LedgerSchemaError("$", "default ledger schema file is missing")

    def validate(self, value: object) -> None:
        self._validate(value, self._schema, "$", ref_stack=())

    def _validate(
        self,
        value: object,
        schema: Mapping[str, object],
        path: str,
        *,
        ref_stack: tuple[str, ...],
    ) -> None:
        reference = schema.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise LedgerSchemaError(path, "$ref must be a string")
            if reference in ref_stack:
                raise LedgerSchemaError(path, "recursive schema reference")
            target = self._resolve_reference(reference)
            self._validate(
                value,
                target,
                path,
                ref_stack=ref_stack + (reference,),
            )
            return

        alternatives = schema.get("oneOf")
        if alternatives is not None:
            if not isinstance(alternatives, list):
                raise LedgerSchemaError(path, "oneOf must be an array")
            matches = 0
            for alternative in alternatives:
                if not isinstance(alternative, Mapping):
                    raise LedgerSchemaError(path, "oneOf item must be an object")
                try:
                    self._validate(value, alternative, path, ref_stack=ref_stack)
                except LedgerSchemaError:
                    continue
                matches += 1
            if matches != 1:
                raise LedgerSchemaError(path, "must match exactly one schema")
            return

        expected_type = schema.get("type")
        if expected_type is not None:
            allowed_types = (
                expected_type if isinstance(expected_type, list) else [expected_type]
            )
            if not all(isinstance(item, str) for item in allowed_types):
                raise LedgerSchemaError(path, "schema type is invalid")
            if not any(_matches_type(value, item) for item in allowed_types):
                raise LedgerSchemaError(path, "has an invalid JSON type")

        if "const" in schema and value != schema["const"]:
            raise LedgerSchemaError(path, "does not match const")
        enum = schema.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or value not in enum:
                raise LedgerSchemaError(path, "is not an allowed enum value")

        if isinstance(value, Mapping):
            self._validate_object(value, schema, path, ref_stack=ref_stack)
        elif isinstance(value, list):
            self._validate_array(value, schema, path, ref_stack=ref_stack)
        elif isinstance(value, str):
            self._validate_string(value, schema, path)
        elif _is_number(value):
            self._validate_number(value, schema, path)

    def _validate_object(
        self,
        value: Mapping[object, object],
        schema: Mapping[str, object],
        path: str,
        *,
        ref_stack: tuple[str, ...],
    ) -> None:
        if any(not isinstance(key, str) for key in value):
            raise LedgerSchemaError(path, "object keys must be strings")
        required = schema.get("required", [])
        if not isinstance(required, list) or any(
            not isinstance(item, str) for item in required
        ):
            raise LedgerSchemaError(path, "schema required list is invalid")
        for field in required:
            if field not in value:
                raise LedgerSchemaError(f"{path}.{field}", "required field is missing")

        properties = schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise LedgerSchemaError(path, "schema properties is invalid")
        if schema.get("additionalProperties") is False:
            unexpected = set(value) - set(properties)
            if unexpected:
                raise LedgerSchemaError(path, "contains an unexpected field")

        for field, child in value.items():
            child_schema = properties.get(field)
            if child_schema is None:
                continue
            if not isinstance(child_schema, Mapping):
                raise LedgerSchemaError(path, "property schema is invalid")
            self._validate(
                child,
                child_schema,
                f"{path}.{field}",
                ref_stack=ref_stack,
            )

    def _validate_array(
        self,
        value: list[object],
        schema: Mapping[str, object],
        path: str,
        *,
        ref_stack: tuple[str, ...],
    ) -> None:
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, Mapping):
                raise LedgerSchemaError(path, "items schema is invalid")
            for index, item in enumerate(value):
                self._validate(
                    item,
                    item_schema,
                    f"{path}[{index}]",
                    ref_stack=ref_stack,
                )
        if schema.get("uniqueItems") is True:
            canonical = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in value
            ]
            if len(canonical) != len(set(canonical)):
                raise LedgerSchemaError(path, "array items must be unique")

    def _validate_string(
        self,
        value: str,
        schema: Mapping[str, object],
        path: str,
    ) -> None:
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise LedgerSchemaError(path, "string is shorter than minLength")
        if isinstance(maximum, int) and len(value) > maximum:
            raise LedgerSchemaError(path, "string is longer than maxLength")

        value_format = schema.get("format")
        if value_format == "uuid":
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError) as error:
                raise LedgerSchemaError(path, "string is not a UUID") from error
        elif value_format == "date-time":
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as error:
                raise LedgerSchemaError(path, "string is not a date-time") from error
            if parsed.tzinfo is None:
                raise LedgerSchemaError(path, "date-time must include a timezone")

    @staticmethod
    def _validate_number(
        value: int | float,
        schema: Mapping[str, object],
        path: str,
    ) -> None:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if _is_number(minimum) and value < minimum:
            raise LedgerSchemaError(path, "number is below minimum")
        if _is_number(maximum) and value > maximum:
            raise LedgerSchemaError(path, "number is above maximum")

    def _resolve_reference(self, reference: str) -> Mapping[str, object]:
        if not reference.startswith("#/"):
            raise LedgerSchemaError("$", "only local schema references are supported")
        current: object = self._schema
        for raw_component in reference[2:].split("/"):
            component = raw_component.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, Mapping) or component not in current:
                raise LedgerSchemaError("$", "schema reference cannot be resolved")
            current = current[component]
        if not isinstance(current, Mapping):
            raise LedgerSchemaError("$", "schema reference target is not an object")
        return current


def _matches_type(value: object, expected: str) -> bool:
    return {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": _is_number(value),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, False)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _json_object_copy(value: Mapping[str, object], name: str) -> dict[str, object]:
    try:
        copied = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise LedgerSchemaError("$", f"{name} is not JSON") from error
    if not isinstance(copied, dict):
        raise LedgerSchemaError("$", f"{name} must be an object")
    return copied
