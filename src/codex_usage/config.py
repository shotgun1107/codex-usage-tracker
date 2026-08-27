"""Local, non-secret application configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
import uuid


class ConfigError(ValueError):
    """Local configuration is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class AppConfig:
    schema_version: int
    device_id: str
    codex_home: str
    state_db: str
    ledger_root: str
    credential_target: str
    key_id: str

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ConfigError("unsupported config schema_version")
        try:
            canonical_device = str(uuid.UUID(self.device_id))
        except (ValueError, AttributeError) as error:
            raise ConfigError("device_id must be a UUID") from error
        if canonical_device != self.device_id:
            raise ConfigError("device_id must use canonical UUID text")
        for field_name in ("codex_home", "state_db", "ledger_root"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ConfigError(f"{field_name} must not be empty")
            if not Path(value).is_absolute():
                raise ConfigError(f"{field_name} must be an absolute path")
        if (
            not self.key_id.startswith("key_h1_")
            or len(self.key_id.removeprefix("key_h1_")) != 22
        ):
            raise ConfigError("key_id is invalid")
        if self.credential_target != f"CodexUsageTracker/{self.key_id}":
            raise ConfigError("credential_target does not match key_id")


def default_config_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / "codex-usage-tracker" / "config.json"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError("configuration does not exist; run init first") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError("configuration cannot be read") from error
    if not isinstance(value, dict):
        raise ConfigError("configuration root must be an object")
    expected = {field.name for field in AppConfig.__dataclass_fields__.values()}
    if set(value) != expected:
        raise ConfigError("configuration fields do not match schema v1")
    try:
        return AppConfig(**value)
    except TypeError as error:
        raise ConfigError("configuration values are invalid") from error


def save_config(
    config: AppConfig,
    path: str | Path | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    config_path = Path(path).expanduser().resolve() if path else default_config_path()
    if config_path.exists() and not overwrite:
        raise ConfigError("configuration already exists")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        asdict(config),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, config_path)
        if os.name != "nt":
            config_path.chmod(0o600)
        return config_path
    except OSError as error:
        raise ConfigError("configuration cannot be written") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
