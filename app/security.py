from __future__ import annotations

import base64
import ctypes
import os
import sys
from ctypes import wintypes

_ENTROPY = b"AgniaBlueskySuite/passwords/v1"
_DPAPI_PREFIX = "dpapi:"
_PORTABLE_PREFIX = "portable-test:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _make_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    return blob, buffer


def _protect_windows(data: bytes) -> bytes:
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(_ENTROPY)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    protect = crypt32.CryptProtectData
    protect.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    protect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    ok = protect(
        ctypes.byref(in_blob),
        "Agnia Bluesky Suite",
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise SecretError(f"DPAPI encryption failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        local_free(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def _unprotect_windows(data: bytes) -> bytes:
    in_blob, in_buffer = _make_blob(data)
    entropy_blob, entropy_buffer = _make_blob(_ENTROPY)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    unprotect = crypt32.CryptUnprotectData
    unprotect.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    unprotect.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    ok = unprotect(
        ctypes.byref(in_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    _ = (in_buffer, entropy_buffer)
    if not ok:
        raise SecretError(f"DPAPI decryption failed: {ctypes.WinError()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        local_free(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if sys.platform == "win32":
        protected = _protect_windows(raw)
        return _DPAPI_PREFIX + base64.b64encode(protected).decode("ascii")
    # Only used by tests/source runs on non-Windows. Release builds always use DPAPI.
    return _PORTABLE_PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect_secret(stored: str | None) -> str:
    if not stored:
        return ""
    if stored.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise SecretError("A Windows DPAPI secret cannot be opened on this platform")
        payload = base64.b64decode(stored[len(_DPAPI_PREFIX):])
        return _unprotect_windows(payload).decode("utf-8")
    if stored.startswith(_PORTABLE_PREFIX):
        if sys.platform == "win32" and not os.getenv("AGNIA_BLUESKY_ALLOW_PORTABLE_SECRET"):
            raise SecretError("Portable test secret is disabled on Windows")
        return base64.b64decode(stored[len(_PORTABLE_PREFIX):]).decode("utf-8")
    raise SecretError("Unknown secret format")


def secret_is_dpapi(stored: str | None) -> bool:
    return bool(stored and stored.startswith(_DPAPI_PREFIX))
