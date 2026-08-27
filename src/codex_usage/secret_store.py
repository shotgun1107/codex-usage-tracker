"""Shared HMAC key storage backed by Windows Credential Manager."""

from __future__ import annotations

import base64
from collections.abc import MutableMapping
import ctypes
from ctypes import wintypes
import os
from typing import Protocol

from codex_usage.privacy.identifiers import MINIMUM_KEY_BYTES


class SecretStoreError(RuntimeError):
    """A shared secret could not be stored or retrieved safely."""


class SecretStoreUnavailable(SecretStoreError):
    """The operating-system secret store is unavailable in this logon context."""

    def __init__(self, message: str, error_code: int) -> None:
        super().__init__(message)
        self.error_code = error_code


class SecretStore(Protocol):
    def get(self, target: str) -> bytes | None: ...

    def put(self, target: str, secret: bytes) -> None: ...

    def delete(self, target: str) -> None: ...


class MemorySecretStore:
    """Test-only in-memory implementation of the secret-store port."""

    def __init__(self, values: MutableMapping[str, bytes] | None = None) -> None:
        self.values = values if values is not None else {}

    def get(self, target: str) -> bytes | None:
        return self.values.get(_validate_target(target))

    def put(self, target: str, secret: bytes) -> None:
        self.values[_validate_target(target)] = _validate_secret(secret)

    def delete(self, target: str) -> None:
        self.values.pop(_validate_target(target), None)


class _CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
    _fields_ = [
        ("Keyword", wintypes.LPWSTR),
        ("Flags", wintypes.DWORD),
        ("ValueSize", wintypes.DWORD),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.POINTER(_CREDENTIAL_ATTRIBUTEW)),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialStore:
    """Store binary key material as a generic per-user Windows credential."""

    _TYPE_GENERIC = 1
    _PERSIST_LOCAL_MACHINE = 2
    _ERROR_NOT_FOUND = 1168

    def __init__(self) -> None:
        if os.name != "nt":
            raise SecretStoreError("Windows Credential Manager is unavailable")
        self._api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None
        self._api.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._api.CredDeleteW.restype = wintypes.BOOL

    def get(self, target: str) -> bytes | None:
        clean_target = _validate_target(target)
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._api.CredReadW(
            clean_target,
            self._TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error_code = ctypes.get_last_error()
            if error_code == self._ERROR_NOT_FOUND:
                return None
            raise SecretStoreError("Credential Manager read failed") from ctypes.WinError(
                error_code
            )
        try:
            credential = pointer.contents
            secret = ctypes.string_at(
                credential.CredentialBlob,
                credential.CredentialBlobSize,
            )
            return _validate_secret(secret)
        finally:
            self._api.CredFree(pointer)

    def put(self, target: str, secret: bytes) -> None:
        clean_target = _validate_target(target)
        clean_secret = _validate_secret(secret)
        buffer = (ctypes.c_ubyte * len(clean_secret)).from_buffer_copy(clean_secret)
        credential = _CREDENTIALW()
        credential.Type = self._TYPE_GENERIC
        credential.TargetName = clean_target
        credential.CredentialBlobSize = len(clean_secret)
        credential.CredentialBlob = ctypes.cast(
            buffer,
            ctypes.POINTER(ctypes.c_ubyte),
        )
        credential.Persist = self._PERSIST_LOCAL_MACHINE
        credential.UserName = "Codex Usage Tracker"
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            error_code = ctypes.get_last_error()
            if error_code == 1312:
                raise SecretStoreUnavailable(
                    "Credential Manager has no usable logon session",
                    error_code,
                )
            raise SecretStoreError("Credential Manager write failed") from ctypes.WinError(
                error_code
            )

    def delete(self, target: str) -> None:
        clean_target = _validate_target(target)
        if self._api.CredDeleteW(clean_target, self._TYPE_GENERIC, 0):
            return
        error_code = ctypes.get_last_error()
        if error_code != self._ERROR_NOT_FOUND:
            raise SecretStoreError("Credential Manager delete failed") from ctypes.WinError(
                error_code
            )


def default_secret_store() -> SecretStore:
    return WindowsCredentialStore()


def encode_recovery_key(secret: bytes) -> str:
    clean_secret = _validate_secret(secret)
    return base64.urlsafe_b64encode(clean_secret).rstrip(b"=").decode("ascii")


def decode_recovery_key(value: str) -> bytes:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SecretStoreError("recovery key must be non-empty and trimmed")
    padding = "=" * (-len(value) % 4)
    try:
        secret = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as error:
        raise SecretStoreError("recovery key is not valid base64url") from error
    return _validate_secret(secret)


def _validate_target(target: str) -> str:
    if not isinstance(target, str) or not target or "\x00" in target:
        raise SecretStoreError("credential target is invalid")
    return target


def _validate_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or len(secret) < MINIMUM_KEY_BYTES:
        raise SecretStoreError(
            f"shared secret must contain at least {MINIMUM_KEY_BYTES} bytes"
        )
    return secret
