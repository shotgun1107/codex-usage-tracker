"""Git remote normalization and deterministic project-remote selection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
import ipaddress
import re
import unicodedata
from urllib.parse import urlsplit


_NETWORK_SCHEMES = {"git", "http", "https", "ssh"}
_DEFAULT_PORTS = {"git": 9418, "http": 80, "https": 443, "ssh": 22}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_SCP_REMOTE = re.compile(
    r"^(?:(?P<user>[^@/:\\]+)@)?(?P<host>\[[^\]]+\]|[^/:\\]+):(?P<path>.+)$"
)


class RemoteNormalizationError(ValueError):
    """Raised when a network remote is malformed or unsupported."""


class RemoteResolutionKind(StrEnum):
    """How a project remote was selected."""

    ORIGIN = "self_origin"
    UNIQUE_REMOTE = "unique_remote"
    AMBIGUOUS_REMOTE = "ambiguous_remote"
    LOCAL_ONLY = "local_only"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class RemoteResolution:
    """The normalized result of origin and fallback remote selection."""

    kind: RemoteResolutionKind
    canonical: str | None = None
    candidates: tuple[str, ...] = ()


def normalize_remote(remote: str) -> str | None:
    """Normalize a network Git remote, returning ``None`` for local remotes.

    Authentication data and transport differences are intentionally removed.
    GitHub owner/repository paths are lower-cased because GitHub repository URLs
    are case-insensitive. Path case for other hosts is preserved.
    """

    text = _validate_remote_text(remote)
    if _looks_local(text):
        return None

    if "://" in text:
        host, port, path = _parse_url_remote(text)
    else:
        host, port, path = _parse_scp_remote(text)

    canonical_host = _normalize_host(host)
    canonical_path = _normalize_path(path, lower=canonical_host == "github.com")
    authority_host = (
        f"[{canonical_host}]" if ":" in canonical_host else canonical_host
    )
    authority = authority_host if port is None else f"{authority_host}:{port}"
    return f"{authority}/{canonical_path}"


def resolve_remote(
    origin_url: str | None,
    remote_urls: Iterable[str],
) -> RemoteResolution:
    """Select origin, one unique network remote, or an explicit fallback state."""

    saw_local = False

    if origin_url is not None and origin_url.strip():
        origin = normalize_remote(origin_url)
        if origin is not None:
            return RemoteResolution(
                kind=RemoteResolutionKind.ORIGIN,
                canonical=origin,
                candidates=(origin,),
            )
        saw_local = True

    candidates: set[str] = set()
    for remote_url in remote_urls:
        normalized = normalize_remote(remote_url)
        if normalized is None:
            saw_local = True
        else:
            candidates.add(normalized)

    ordered = tuple(sorted(candidates))
    if len(ordered) == 1:
        return RemoteResolution(
            kind=RemoteResolutionKind.UNIQUE_REMOTE,
            canonical=ordered[0],
            candidates=ordered,
        )
    if len(ordered) > 1:
        return RemoteResolution(
            kind=RemoteResolutionKind.AMBIGUOUS_REMOTE,
            candidates=ordered,
        )
    if saw_local:
        return RemoteResolution(kind=RemoteResolutionKind.LOCAL_ONLY)
    return RemoteResolution(kind=RemoteResolutionKind.UNCLASSIFIED)


def _validate_remote_text(remote: str) -> str:
    if not isinstance(remote, str):
        raise RemoteNormalizationError("remote must be a string")
    text = remote.strip()
    if not text:
        raise RemoteNormalizationError("remote must not be empty")
    return text


def _looks_local(remote: str) -> bool:
    lowered = remote.lower()
    return (
        lowered.startswith("file:")
        or bool(_WINDOWS_DRIVE.match(remote))
        or remote.startswith(("/", "\\", "./", "../", "~/", ".\\", "..\\"))
        or ("://" not in remote and _SCP_REMOTE.match(remote) is None)
    )


def _parse_url_remote(remote: str) -> tuple[str, int | None, str]:
    parsed = urlsplit(remote)
    scheme = parsed.scheme.lower()
    if scheme == "file":
        raise AssertionError("file remotes must be handled before URL parsing")
    if scheme not in _NETWORK_SCHEMES:
        raise RemoteNormalizationError(f"unsupported remote scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise RemoteNormalizationError("network remote must include a host")
    try:
        port = parsed.port
    except ValueError as error:
        raise RemoteNormalizationError("remote contains an invalid port") from error
    if port == _DEFAULT_PORTS.get(scheme):
        port = None
    return parsed.hostname, port, parsed.path


def _parse_scp_remote(remote: str) -> tuple[str, None, str]:
    match = _SCP_REMOTE.fullmatch(remote)
    if match is None:
        raise RemoteNormalizationError("remote is neither a network URL nor scp syntax")
    host = match.group("host")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    path = match.group("path").split("?", 1)[0].split("#", 1)[0]
    return host, None, path


def _normalize_host(host: str) -> str:
    clean_host = host.strip().rstrip(".")
    if not clean_host or any(character.isspace() for character in clean_host):
        raise RemoteNormalizationError("remote host is empty or contains whitespace")
    if ":" in clean_host:
        try:
            return ipaddress.IPv6Address(clean_host).compressed
        except ipaddress.AddressValueError as error:
            raise RemoteNormalizationError("remote contains an invalid IPv6 host") from error
    try:
        return clean_host.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise RemoteNormalizationError("remote host is not valid IDNA") from error


def _normalize_path(path: str, *, lower: bool) -> str:
    normalized = unicodedata.normalize("NFC", path.replace("\\", "/"))
    components = [component for component in normalized.split("/") if component]
    if any(component in {".", ".."} for component in components):
        raise RemoteNormalizationError("remote path must not contain dot segments")
    if not components:
        raise RemoteNormalizationError("remote must include a repository path")

    final = components[-1]
    if final.lower().endswith(".git"):
        final = final[:-4]
    if not final:
        raise RemoteNormalizationError("remote repository name must not be empty")
    components[-1] = final

    canonical = "/".join(components)
    return canonical.lower() if lower else canonical
