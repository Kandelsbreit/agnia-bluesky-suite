from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bluesky import BlueskyError, BlueskyGateway


class ResponseError(RuntimeError):
    def __init__(self, message="request failed", status=500, code="InternalError"):
        super().__init__(message)
        self.response = SimpleNamespace(
            status_code=status,
            headers={},
            content={"error": code, "message": message},
        )


class FakeClient:
    def __init__(self, *, fail_create=False, record_exists=False):
        self.fail_create = fail_create
        self.record_exists = record_exists
        self.created = []
        self.app = SimpleNamespace(
            bsky=SimpleNamespace(
                feed=SimpleNamespace(post=SimpleNamespace(create=self.create_post))
            )
        )
        self.com = SimpleNamespace(
            atproto=SimpleNamespace(repo=SimpleNamespace(get_record=self.get_record))
        )

    def login(self, handle, password):
        assert password == "app-password"
        return SimpleNamespace(handle=handle, did="did:plc:me", display_name="Me")

    @staticmethod
    def get_current_time_iso():
        return "2026-01-01T00:00:00.000Z"

    def create_post(self, repo, record, rkey):
        self.created.append((repo, record.text, rkey))
        if self.fail_create:
            raise ResponseError("temporary", 503)
        return SimpleNamespace(uri=f"at://{repo}/app.bsky.feed.post/{rkey}", cid="new-cid")

    def get_record(self, params):
        if not self.record_exists:
            raise ResponseError("not found", 404, "RecordNotFound")
        return SimpleNamespace(
            uri=f"at://{params.repo}/{params.collection}/{params.rkey}",
            cid="existing-cid",
            value={"text": "Hello"},
        )


def test_publish_uses_caller_supplied_record_key():
    client = FakeClient()
    gateway = BlueskyGateway("Me.Test", "app-password", client_factory=lambda **_kwargs: client)
    result = gateway.publish_text("Hello", "fixed-record-key")
    assert client.created == [("did:plc:me", "Hello", "fixed-record-key")]
    assert result.uri.endswith("/fixed-record-key")
    assert result.recovered_existing is False


def test_publish_recovers_existing_record_after_uncertain_error():
    client = FakeClient(fail_create=True, record_exists=True)
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    result = gateway.publish_text("Hello", "same-key")
    assert result.recovered_existing is True
    assert result.cid == "existing-cid"
    assert client.created[0][2] == "same-key"


def test_publish_keeps_retryable_error_if_record_cannot_be_verified():
    client = FakeClient(fail_create=True, record_exists=False)
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    with pytest.raises(BlueskyError) as caught:
        gateway.publish_text("Hello", "same-key")
    assert caught.value.retryable is True
    assert str(caught.value) == "temporary"


def test_publish_validates_empty_and_grapheme_limit_before_api_call():
    client = FakeClient()
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    with pytest.raises(BlueskyError, match="Пустой"):
        gateway.publish_text("   ", "key")
    with pytest.raises(BlueskyError, match="301/300"):
        gateway.publish_text("x" * 301, "key")
    with pytest.raises(BlueskyError, match="3300/3000"):
        gateway.publish_text("👩‍💻" * 300, "key")
    assert client.created == []


def test_missing_credentials_is_explicit_auth_error():
    with pytest.raises(BlueskyError) as caught:
        BlueskyGateway("me.test", "").login()
    assert caught.value.auth_error is True
