from __future__ import annotations

import sys

import pytest

from app.security import SecretError, protect_secret, secret_is_dpapi, unprotect_secret


@pytest.mark.skipif(sys.platform == "win32", reason="portable source-mode behavior is non-Windows only")
def test_portable_source_mode_secret_roundtrip_is_not_plaintext():
    stored = protect_secret("abcd-efgh-ijkl-mnop")
    assert stored.startswith("portable-test:")
    assert "abcd-efgh" not in stored
    assert unprotect_secret(stored) == "abcd-efgh-ijkl-mnop"
    assert not secret_is_dpapi(stored)


def test_empty_and_unknown_secret():
    assert protect_secret("") == ""
    assert unprotect_secret("") == ""
    with pytest.raises(SecretError):
        unprotect_secret("plain-password")

