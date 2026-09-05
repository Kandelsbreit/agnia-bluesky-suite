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
            bsky=SimpleNamespace(feed=SimpleNamespace(post=SimpleNamespace(create=self.create_post)))
        )
        self.com = SimpleNamespace(atproto=SimpleNamespace(repo=SimpleNamespace(get_record=self.get_record)))

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
    assert client.created == []  # Verification happens before any duplicate create request.


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


class ReauthClient:
    def __init__(self):
        self.login_count = 0
        self.post_attempts = 0
        self.app = SimpleNamespace(
            bsky=SimpleNamespace(feed=SimpleNamespace(post=SimpleNamespace(create=self.create_post)))
        )
        self.com = SimpleNamespace(atproto=SimpleNamespace(repo=SimpleNamespace(get_record=self.get_record)))

    def login(self, handle, password):
        self.login_count += 1
        return SimpleNamespace(handle=handle, did="did:plc:me", display_name="Me")

    @staticmethod
    def get_current_time_iso():
        return "2026-01-01T00:00:00.000Z"

    def create_post(self, repo, record, rkey):
        self.post_attempts += 1
        if self.post_attempts == 1:
            raise ResponseError("Token has expired", 400, "ExpiredToken")
        return SimpleNamespace(uri=f"at://{repo}/app.bsky.feed.post/{rkey}", cid="fresh-cid")

    def get_record(self, params):
        raise ResponseError("not found", 404, "RecordNotFound")


def test_publish_reauthenticates_automatically_on_expired_token():
    client = ReauthClient()
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    result = gateway.publish_text("Hello fresh", "rkey-fresh")
    assert result.cid == "fresh-cid"
    assert client.login_count == 2
    assert client.post_attempts == 2


def test_extract_facets_byte_indices_and_tags():
    from app.bluesky import extract_facets

    text = "Spread wide and waiting. #ts #питер https://example.com/test"
    facets = extract_facets(text)
    assert facets is not None
    assert len(facets) == 3

    # 1. #ts
    f0 = facets[0]
    assert f0.features[0].tag == "ts"
    assert text.encode("utf-8")[f0.index.byte_start : f0.index.byte_end].decode("utf-8") == "#ts"

    # 2. #питер (Cyrillic UTF-8 multi-byte)
    f1 = facets[1]
    assert f1.features[0].tag == "питер"
    assert text.encode("utf-8")[f1.index.byte_start : f1.index.byte_end].decode("utf-8") == "#питер"

    # 3. URL
    f2 = facets[2]
    assert f2.features[0].uri == "https://example.com/test"
    assert text.encode("utf-8")[f2.index.byte_start : f2.index.byte_end].decode("utf-8") == "https://example.com/test"


def test_publish_passes_facets_to_record():
    records_received = []

    class FacetInspectClient(FakeClient):
        def create_post(self, repo, record, rkey):
            records_received.append(record)
            return super().create_post(repo, record, rkey)

    client = FacetInspectClient()
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    gateway.publish_text("Waiting for you... #nsfw #trans", "facet-key")

    assert len(records_received) == 1
    rec = records_received[0]
    assert rec.facets is not None
    assert len(rec.facets) == 2
    assert rec.facets[0].features[0].tag == "nsfw"
    assert rec.facets[1].features[0].tag == "trans"


def test_get_author_recent_posts():
    class FeedClient(FakeClient):
        def get_author_feed(self, actor, limit=100, cursor=None, filter=None):
            return SimpleNamespace(
                feed=[
                    SimpleNamespace(
                        post=SimpleNamespace(
                            uri="at://did:plc:me/app.bsky.feed.post/post1",
                            cid="cid1",
                            record=SimpleNamespace(
                                text="Hello recent post #1",
                                createdAt="2026-09-05T08:00:00Z",
                            ),
                        )
                    )
                ],
                cursor=None,
            )

    client = FeedClient()
    gateway = BlueskyGateway("me.test", "app-password", client_factory=lambda **_kwargs: client)
    posts = gateway.get_author_recent_posts("me.test")
    assert len(posts) == 1
    assert posts[0]["text"] == "Hello recent post #1"
    assert posts[0]["uri"] == "at://did:plc:me/app.bsky.feed.post/post1"
    assert posts[0]["rkey"] == "post1"
