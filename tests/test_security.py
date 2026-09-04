from __future__ import annotations

import pytest

from app.security import SecretError, protect_secret, secret_is_dpapi, secret_is_portable, unprotect_secret


def test_portable_secret_roundtrip_is_authenticated_and_cross_platform():
    stored = protect_secret("abcd-efgh-ijkl-mnop")
    assert stored.startswith("portable-v2:")
    assert "abcd-efgh" not in stored
    assert unprotect_secret(stored) == "abcd-efgh-ijkl-mnop"
    assert not secret_is_dpapi(stored)
    assert secret_is_portable(stored)


def test_tampered_secret_raises_secret_error():
    stored = protect_secret("secret-password")
    # Mutate a character in base64 payload
    tampered = stored[:-2] + ("A" if stored[-2] != "A" else "B") + stored[-1]
    with pytest.raises(SecretError):
        unprotect_secret(tampered)


def test_empty_and_unknown_secret():
    assert protect_secret("") == ""
    assert unprotect_secret("") == ""
    with pytest.raises(SecretError):
        unprotect_secret("plain-password")


