from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from atproto import Client, models
from atproto_client.exceptions import (
    InvokeTimeoutError,
    NetworkError,
    UnauthorizedError,
)

from app.utils import is_valid_tid, new_record_key, normalize_handle, post_validation_error

DISCOVER_FEED_URI = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
PUBLIC_SERVICE = "https://public.api.bsky.app"
POST_COLLECTION = "app.bsky.feed.post"


@dataclass(frozen=True)
class Profile:
    handle: str
    did: str
    display_name: str
    avatar: str = ""
    posts_count: int = 0


@dataclass(frozen=True)
class PublishResult:
    uri: str
    cid: str
    recovered_existing: bool = False


@dataclass(frozen=True)
class ActionResult:
    performed: bool
    skipped: bool
    message: str
    target_handle: str = ""
    target_key: str = ""


class BlueskyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        auth_error: bool = False,
        rate_limited: bool = False,
        retry_after: int | None = None,
        code: str = "",
    ):
        super().__init__(message)
        self.retryable = retryable
        self.auth_error = auth_error
        self.rate_limited = rate_limited
        self.retry_after = retry_after
        self.code = code


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    raise TypeError(f"Unsupported Bluesky response type: {type(value).__name__}")


DEFAULT_REQUEST_TIMEOUT = 25.0


def _error_details(exc: Exception) -> BlueskyError:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    content = getattr(response, "content", None)
    code = str(_field(content, "error", "") or "")
    message = str(_field(content, "message", "") or "").strip()
    if not message:
        message = str(exc).strip() or exc.__class__.__name__
    retry_after = None
    header = headers.get("retry-after") or headers.get("Retry-After")
    if header:
        try:
            retry_after = max(1, int(float(header)))
        except (TypeError, ValueError):
            pass
    code_lower = code.lower()
    msg_lower = message.lower()
    auth_error = (
        isinstance(exc, UnauthorizedError)
        or status in {401, 403}
        or "expiredtoken" in code_lower
        or "invalidtoken" in code_lower
        or "token has expired" in msg_lower
        or "authentication required" in msg_lower
    )
    rate_limited = status == 429 or code_lower in {"ratelimitexceeded", "ratelimited"}
    retryable = (
        isinstance(exc, NetworkError | InvokeTimeoutError)
        or rate_limited
        or bool(status and status >= 500)
    )
    return BlueskyError(
        message,
        retryable=retryable,
        auth_error=auth_error,
        rate_limited=rate_limited,
        retry_after=retry_after,
        code=code,
    )


class BlueskyGateway:
    """One API surface used by posting, queueing, automation and export."""

    def __init__(
        self,
        handle: str = "",
        app_password: str = "",
        *,
        client_factory: Callable[..., Client] = Client,
    ):
        self.handle = normalize_handle(handle)
        self.app_password = app_password.strip()
        self._client_factory = client_factory
        self._client: Client | None = None
        self._profile: Profile | None = None
        self._lock = threading.RLock()

    @property
    def profile(self) -> Profile | None:
        return self._profile

    def _new_client(self, public: bool = False) -> Client:
        kwargs: dict[str, Any] = {}
        if public:
            kwargs["base_url"] = PUBLIC_SERVICE
        try:
            return self._client_factory(request_timeout=DEFAULT_REQUEST_TIMEOUT, **kwargs)
        except TypeError:
            try:
                return self._client_factory(**kwargs)
            except TypeError:
                return self._client_factory()

    def login(self, force: bool = False) -> Profile:
        if not self.handle or not self.app_password:
            raise BlueskyError("Не указан handle или App Password", auth_error=True)
        with self._lock:
            if self._client is not None and self._profile is not None and not force:
                return self._profile
            try:
                client = self._new_client()
                raw = client.login(self.handle, self.app_password)
                self._client = client
                self._profile = Profile(
                    handle=str(_field(raw, "handle", self.handle)),
                    did=str(_field(raw, "did", "")),
                    display_name=str(_field(raw, "display_name", "") or _field(raw, "handle", self.handle)),
                    avatar=str(_field(raw, "avatar", "") or ""),
                    posts_count=int(_field(raw, "posts_count", 0) or 0),
                )
                return self._profile
            except Exception as exc:
                raise _error_details(exc) from exc

    def test_connection(self) -> Profile:
        return self.login(force=True)

    def _authenticated(self) -> Client:
        self.login()
        assert self._client is not None
        return self._client

    def _with_reauth(self, action: Callable[[Client], Any]) -> Any:
        client = self._authenticated()
        try:
            return action(client)
        except Exception as exc:
            details = _error_details(exc)
            if details.auth_error and self.handle and self.app_password:
                with self._lock:
                    self._client = None
                self.login(force=True)
                assert self._client is not None
                return action(self._client)
            raise details from exc

    def resolve_profile(self, actor: str | None = None) -> Profile:
        clean = normalize_handle(actor or self.handle)
        if not clean:
            raise BlueskyError("Не указан аккаунт")
        try:
            client = self._client or self._new_client(public=True)
            raw = client.get_profile(actor=clean)
            return Profile(
                handle=str(_field(raw, "handle", clean)),
                did=str(_field(raw, "did", "")),
                display_name=str(_field(raw, "display_name", "") or _field(raw, "handle", clean)),
                avatar=str(_field(raw, "avatar", "") or ""),
                posts_count=int(_field(raw, "posts_count", 0) or 0),
            )
        except Exception as exc:
            raise _error_details(exc) from exc

    def get_timeline(self, limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
        def action(client: Client):
            response = client.get_timeline(limit=min(100, max(1, limit)), cursor=cursor)
            return [_to_dict(item) for item in response.feed], response.cursor
        try:
            return self._with_reauth(action)
        except BlueskyError:
            raise
        except Exception as exc:
            raise _error_details(exc) from exc

    def get_discover_feed(self, limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
        params: dict[str, Any] = {"feed": DISCOVER_FEED_URI, "limit": min(100, max(1, limit))}
        if cursor:
            params["cursor"] = cursor
        def action(client: Client):
            response = client.app.bsky.feed.get_feed(params=params)
            return [_to_dict(item) for item in response.feed], response.cursor
        try:
            return self._with_reauth(action)
        except BlueskyError:
            raise
        except Exception as exc:
            raise _error_details(exc) from exc

    def search_posts(self, query: str, limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
        params: dict[str, Any] = {"q": query.strip(), "limit": min(100, max(1, limit))}
        if cursor:
            params["cursor"] = cursor
        def action(client: Client):
            response = client.app.bsky.feed.search_posts(params=params)
            return [{"post": _to_dict(post)} for post in response.posts], response.cursor
        try:
            return self._with_reauth(action)
        except BlueskyError:
            raise
        except Exception as exc:
            raise _error_details(exc) from exc

    def like(self, uri: str, cid: str) -> ActionResult:
        def action(client: Client):
            client.like(uri=uri, cid=cid)
            return ActionResult(True, False, "Лайк поставлен", target_key=uri)
        try:
            return self._with_reauth(action)
        except BlueskyError as exc:
            lowered = f"{exc.code} {exc}".lower()
            if "already" in lowered and "like" in lowered:
                return ActionResult(False, True, "Пост уже лайкнут", target_key=uri)
            raise
        except Exception as exc:
            details = _error_details(exc)
            lowered = f"{details.code} {details}".lower()
            if "already" in lowered and "like" in lowered:
                return ActionResult(False, True, "Пост уже лайкнут", target_key=uri)
            raise details from exc

    def follow(self, target: str) -> ActionResult:
        clean = normalize_handle(target)
        if not clean:
            return ActionResult(False, True, "Пустой handle")
        target_did = ""
        target_handle = clean
        def action(client: Client):
            nonlocal target_did, target_handle
            profile = client.get_profile(actor=clean)
            target_did = str(_field(profile, "did", ""))
            target_handle = str(_field(profile, "handle", clean))
            own_did = self._profile.did if self._profile else ""
            if target_did == own_did:
                return ActionResult(False, True, "Свой аккаунт пропущен", target_handle, target_did)
            viewer = _field(profile, "viewer", None)
            if viewer and _field(viewer, "following", None):
                return ActionResult(False, True, "Уже есть подписка", target_handle, target_did)
            client.follow(subject=target_did)
            return ActionResult(True, False, "Подписка оформлена", target_handle, target_did)
        try:
            return self._with_reauth(action)
        except BlueskyError as exc:
            lowered = f"{exc.code} {exc}".lower()
            if "already" in lowered and ("follow" in lowered or "following" in lowered):
                return ActionResult(False, True, "Уже есть подписка", target_handle, target_did)
            raise
        except Exception as exc:
            details = _error_details(exc)
            lowered = f"{details.code} {details}".lower()
            if "already" in lowered and ("follow" in lowered or "following" in lowered):
                return ActionResult(False, True, "Уже есть подписка", target_handle, target_did)
            raise details from exc

    def _verify_record(self, record_key: str, expected_text: str) -> PublishResult | None:
        if not self._profile:
            return None
        def action(client: Client):
            response = client.com.atproto.repo.get_record(
                models.ComAtprotoRepoGetRecord.Params(
                    repo=self._profile.did,
                    collection=POST_COLLECTION,
                    rkey=record_key,
                )
            )
            actual_text = str(_field(_field(response, "value", {}), "text", ""))
            if actual_text != expected_text:
                return None
            return PublishResult(
                uri=str(_field(response, "uri", f"at://{self._profile.did}/{POST_COLLECTION}/{record_key}")),
                cid=str(_field(response, "cid", "") or ""),
                recovered_existing=True,
            )
        try:
            return self._with_reauth(action)
        except Exception:
            return None

    def publish_text(self, text: str, record_key: str | None = None) -> PublishResult:
        clean_text = text.strip()
        validation_error = post_validation_error(clean_text)
        if validation_error:
            raise BlueskyError(validation_error)

        valid_key = record_key if is_valid_tid(record_key) else new_record_key()

        def action(client: Client) -> PublishResult:
            assert self._profile is not None
            record = models.AppBskyFeedPost.Record(
                text=clean_text,
                created_at=client.get_current_time_iso(),
            )
            try:
                response = client.app.bsky.feed.post.create(
                    self._profile.did,
                    record,
                    rkey=valid_key,
                )
            except Exception as inner_exc:
                inner_msg = str(inner_exc).lower()
                if "invalid tid" in inner_msg or "invalid record key" in inner_msg:
                    response = client.send_post(text=clean_text)
                else:
                    raise
            return PublishResult(uri=str(response.uri), cid=str(response.cid))

        try:
            return self._with_reauth(action)
        except BlueskyError as exc:
            lowered = f"{exc.code} {exc}".lower()
            might_exist = (
                "already" in lowered
                or "recordexists" in lowered
                or exc.retryable
            )
            if might_exist:
                recovered = self._verify_record(valid_key, clean_text)
                if recovered:
                    return recovered
            raise
        except Exception as exc:
            details = _error_details(exc)
            lowered = f"{details.code} {details}".lower()
            might_exist = (
                "already" in lowered
                or "recordexists" in lowered
                or isinstance(exc, NetworkError | InvokeTimeoutError)
                or details.retryable
            )
            if might_exist:
                recovered = self._verify_record(valid_key, clean_text)
                if recovered:
                    return recovered
            raise details from exc

    def fetch_author_feed(
        self,
        actor: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[dict], str | None]:
        try:
            client = self._client or self._new_client(public=True)
            response = client.get_author_feed(
                actor=actor,
                limit=min(100, max(1, limit)),
                cursor=cursor,
                filter="posts_with_replies",
            )
            return [_to_dict(item) for item in response.feed], response.cursor
        except Exception as exc:
            raise _error_details(exc) from exc
