from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import secrets
import sys
from ctypes import wintypes

_ENTROPY = b"AgniaBlueskySuite/passwords/v1"
_DPAPI_PREFIX = "dpapi:"
_PORTABLE_PREFIX = "portable-test:"
_PORTABLE_V2_PREFIX = "portable-v2:"
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


def _get_or_create_master_key() -> bytes:
    from app.paths import data_dir

    key_file = data_dir() / ".secret_key"
    if key_file.exists():
        try:
            content = key_file.read_bytes()
            if len(content) == 32:
                return content
        except OSError:
            pass
    key = secrets.token_bytes(32)
    try:
        key_file.write_bytes(key)
        if sys.platform == "win32":
            try:
                ctypes.windll.kernel32.SetFileAttributesW(str(key_file), 0x02)  # FILE_ATTRIBUTE_HIDDEN
            except Exception:
                pass
    except OSError:
        pass
    return key


def _keystream(key: bytes, iv: bytes, length: int) -> bytes:
    blocks = []
    idx = 0
    gen = 0
    while gen < length:
        block = hmac.new(key, iv + idx.to_bytes(4, "big"), hashlib.sha256).digest()
        blocks.append(block)
        gen += len(block)
        idx += 1
    return b"".join(blocks)[:length]


def _encrypt_portable_v2(data: bytes, master_key: bytes | None = None) -> bytes:
    if master_key is None:
        master_key = _get_or_create_master_key()
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", master_key, salt, iterations=10_000, dklen=64)
    enc_key = derived[:32]
    mac_key = derived[32:]
    stream = _keystream(enc_key, iv, len(data))
    ciphertext = bytes(a ^ b for a, b in zip(data, stream))
    tag = hmac.new(mac_key, salt + iv + ciphertext, hashlib.sha256).digest()
    return salt + iv + tag + ciphertext


def _decrypt_portable_v2(payload: bytes, master_key: bytes | None = None) -> bytes:
    if len(payload) < 16 + 16 + 32:
        raise SecretError("Invalid portable secret payload length")
    if master_key is None:
        master_key = _get_or_create_master_key()
    salt = payload[:16]
    iv = payload[16:32]
    tag = payload[32:64]
    ciphertext = payload[64:]
    derived = hashlib.pbkdf2_hmac("sha256", master_key, salt, iterations=10_000, dklen=64)
    enc_key = derived[:32]
    mac_key = derived[32:]
    expected_tag = hmac.new(mac_key, salt + iv + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise SecretError("Portable secret authentication failed")
    stream = _keystream(enc_key, iv, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    protected = _encrypt_portable_v2(raw)
    return _PORTABLE_V2_PREFIX + base64.b64encode(protected).decode("ascii")


def unprotect_secret(stored: str | None) -> str:
    if not stored:
        return ""
    if stored.startswith(_PORTABLE_V2_PREFIX):
        payload = base64.b64decode(stored[len(_PORTABLE_V2_PREFIX):])
        return _decrypt_portable_v2(payload).decode("utf-8")
    if stored.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise SecretError("Windows DPAPI secret cannot be decrypted on non-Windows")
        try:
            payload = base64.b64decode(stored[len(_DPAPI_PREFIX):])
            return _unprotect_windows(payload).decode("utf-8")
        except Exception as exc:
            raise SecretError(f"DPAPI decryption failed: {exc}") from exc
    if stored.startswith(_PORTABLE_PREFIX):
        return base64.b64decode(stored[len(_PORTABLE_PREFIX):]).decode("utf-8")
    raise SecretError("Unknown secret format")


def secret_is_dpapi(stored: str | None) -> bool:
    return bool(stored and stored.startswith(_DPAPI_PREFIX))


def secret_is_portable(stored: str | None) -> bool:
    return bool(stored and (stored.startswith(_PORTABLE_V2_PREFIX) or stored.startswith(_PORTABLE_PREFIX)))

