from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from atproto import Client, models
from atproto_client.exceptions import (
    InvokeTimeoutError,
    NetworkError,
    UnauthorizedError,
)

from app.utils import new_record_key, normalize_handle, normalize_text, post_validation_error, utcnow_iso

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
        return {k: _to_dict(v) if isinstance(v, dict) or hasattr(v, "__dict__") else v for k, v in value.items()}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(by_alias=True)
    if hasattr(value, "__dict__"):
        return _to_dict(vars(value))
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
            from email.utils import parsedate_to_datetime

            try:
                retry_after = max(1, int(parsedate_to_datetime(header).timestamp() - time.time()))
            except (TypeError, ValueError, OverflowError):
                pass
    reset = headers.get("ratelimit-reset") or headers.get("RateLimit-Reset")
    if status == 429 and reset:
        try:
            retry_after = max(retry_after or 0, 1, int(float(reset) - time.time()))
        except (ValueError, TypeError):
            pass
    code_lower = code.lower()
    msg_lower = message.lower()
    auth_error = (
        isinstance(exc, UnauthorizedError)
        or status == 401
        or "expiredtoken" in code_lower
        or "invalidtoken" in code_lower
        or "token has expired" in msg_lower
        or "authentication required" in msg_lower
    )
    rate_limited = status == 429 or code_lower in {"ratelimitexceeded", "ratelimited"}
    retryable = isinstance(exc, NetworkError | InvokeTimeoutError) or rate_limited or bool(status and status >= 500)
    return BlueskyError(
        message,
        retryable=retryable,
        auth_error=auth_error,
        rate_limited=rate_limited,
        retry_after=retry_after,
        code=code,
    )


def extract_facets(text: str) -> list[models.AppBskyRichtextFacet.Main] | None:
    facets: list[models.AppBskyRichtextFacet.Main] = []
    hashtag_pattern = re.compile(r"(?:^|(?<=\s))#([^\s#.,!?:;()\[\]{}]+)")
    for match in hashtag_pattern.finditer(text):
        tag_word = match.group(1)
        full_tag = "#" + tag_word
        start_char = match.start()
        byte_start = len(text[:start_char].encode("utf-8"))
        byte_end = byte_start + len(full_tag.encode("utf-8"))
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=byte_start, byte_end=byte_end),
                features=[models.AppBskyRichtextFacet.Tag(tag=tag_word)],
            )
        )
    url_pattern = re.compile(r"(?:^|(?<=\s))(https?://[^\s]+)")
    for match in url_pattern.finditer(text):
        url = match.group(1).rstrip(".,!?:;")
        for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
            while url.endswith(right) and url.count(right) > url.count(left):
                url = url[:-1]
        start_char = match.start()
        byte_start = len(text[:start_char].encode("utf-8"))
        byte_end = byte_start + len(url.encode("utf-8"))
        facets.append(
            models.AppBskyRichtextFacet.Main(
                index=models.AppBskyRichtextFacet.ByteSlice(byte_start=byte_start, byte_end=byte_end),
                features=[models.AppBskyRichtextFacet.Link(uri=url)],
            )
        )
    if not facets:
        return None
    facets.sort(key=lambda f: f.index.byte_start)
    return facets


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
        self._recent_posts_cache: list[dict] | None = None
        self._recent_posts_time: float = 0.0
        self._lock = threading.RLock()
        self._cooldown_until = 0.0
        self.session_store = None
        self.session_string = ""

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
                if self.session_store and hasattr(client, "on_session_change"):
                    client.on_session_change(lambda _event, session: self.session_store(session.export()))
                try:
                    raw = (
                        client.login(session_string=self.session_string)
                        if self.session_string and not force
                        else client.login(self.handle, self.app_password)
                    )
                except Exception as exc:
                    if not self.session_string or not _error_details(exc).auth_error:
                        raise
                    self.session_string = ""
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
        with self._lock:
            remaining = int(self._cooldown_until - time.monotonic())
            if remaining > 0:
                raise BlueskyError("Пауза запросов аккаунта", retryable=True, rate_limited=True, retry_after=remaining)
            try:
                client = self._authenticated()
                try:
                    return action(client)
                except Exception as exc:
                    details = _error_details(exc)
                    if details.auth_error and self.handle and self.app_password:
                        self._client = None
                        self.login(force=True)
                        return action(self._client)
                    raise
            except Exception as exc:
                details = exc if isinstance(exc, BlueskyError) else _error_details(exc)
                if details.rate_limited:
                    self._cooldown_until = time.monotonic() + (details.retry_after or 60)
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

    def _verify_record(
        self, record_key: str, expected_text: str, expected_record: dict | None = None
    ) -> PublishResult | None:
        def action(client):
            response = client.com.atproto.repo.get_record(
                models.ComAtprotoRepoGetRecord.Params(
                    repo=self._profile.did, collection=POST_COLLECTION, rkey=record_key
                )
            )
            actual = _to_dict(_field(response, "value", {}))
            if normalize_text(str(actual.get("text", ""))) != normalize_text(expected_text):
                raise BlueskyError(
                    "По этому ключу найден другой пост. Требуется ручная проверка.", code="RecordConflict"
                )
            if expected_record and actual.get("embed") != expected_record.get("embed"):
                raise BlueskyError(
                    "По этому ключу найдены другие вложения. Требуется ручная проверка.", code="RecordConflict"
                )
            return PublishResult(str(_field(response, "uri", "")), str(_field(response, "cid", "")), True)

        try:
            return self._with_reauth(action)
        except BlueskyError as exc:
            if exc.code.lower() in {"recordnotfound", "notfound"}:
                return None
            raise

    def check_recent_post(self, text: str) -> PublishResult | None:
        clean = normalize_text(text)
        if not clean or not self.handle:
            return None
        with self._lock:
            now = time.monotonic()
            if self._recent_posts_cache is None or now - self._recent_posts_time > 60:
                self._recent_posts_cache = self.get_author_recent_posts(self.handle, limit=100)
                self._recent_posts_time = now
            for post in self._recent_posts_cache:
                # Text-only comparison never establishes equivalence with a media post or reply.
                if not post.get("has_embed") and not post.get("is_reply") and normalize_text(post["text"]) == clean:
                    return PublishResult(post["uri"], post["cid"], True)
        return None

    def prepare_record(self, text: str, media: list) -> dict:
        from app.media import media_path, validate_media

        error = post_validation_error(text)
        if error:
            raise BlueskyError(error, code="InvalidRecord")
        validate_media(media)
        client = self._authenticated()
        facets = extract_facets(text.strip()) or []
        # Resolve mentions to stable DIDs. A failed resolution is visible, never silently mislinked.
        for match in re.finditer(r"(?<![\w@])@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z0-9-]+)", text.strip()):
            raw = match.group(1).rstrip(".")
            profile = self._with_reauth(lambda c, raw=raw: c.get_profile(actor=raw))
            start = len(text.strip()[: match.start()].encode())
            end = start + len(("@" + raw).encode())
            if any(f.index.byte_start < end and start < f.index.byte_end for f in facets):
                continue
            facets.append(
                models.AppBskyRichtextFacet.Main(
                    index=models.AppBskyRichtextFacet.ByteSlice(byte_start=start, byte_end=end),
                    features=[models.AppBskyRichtextFacet.Mention(did=str(_field(profile, "did", "")))],
                )
            )
        embed = None
        if media and media[0]["kind"] == "link":
            m = media[0]
            embed = models.AppBskyEmbedExternal.Main(
                external=models.AppBskyEmbedExternal.External(
                    uri=m["uri"], title=m.get("title", ""), description=m.get("description", "")
                )
            )
        elif media:
            uploaded = []
            for m in media:
                blob = self._with_reauth(lambda c, m=m: c.upload_blob(media_path(m).read_bytes())).blob
                if m["kind"] == "video":
                    embed = models.AppBskyEmbedVideo.Main(video=blob, alt=m.get("alt", ""))
                else:
                    aspect = (
                        models.AppBskyEmbedDefs.AspectRatio(width=m["width"], height=m["height"])
                        if m.get("width")
                        else None
                    )
                    uploaded.append(
                        models.AppBskyEmbedImages.Image(image=blob, alt=m.get("alt", ""), aspect_ratio=aspect)
                    )
            if uploaded:
                embed = models.AppBskyEmbedImages.Main(images=uploaded)
        record = models.AppBskyFeedPost.Record(
            text=text.strip(), facets=facets or None, embed=embed, created_at=client.get_current_time_iso()
        )
        return record.model_dump(mode="json", by_alias=True, exclude_none=True)

    def publish_record(self, record: dict, record_key: str) -> PublishResult:
        existing = self._verify_record(record_key, record["text"], record)
        if existing:
            return existing

        def action(client):
            model = models.AppBskyFeedPost.Record.model_validate(record)
            response = client.app.bsky.feed.post.create(self._profile.did, model, rkey=record_key)
            self._recent_posts_cache = None
            return PublishResult(str(response.uri), str(response.cid))

        try:
            return self._with_reauth(action)
        except BlueskyError as exc:
            if exc.retryable or "already" in str(exc).lower() or "recordexists" in exc.code.lower():
                recovered = self._verify_record(record_key, record["text"], record)
                if recovered:
                    return recovered
            raise

    def publish_text(self, text: str, record_key: str | None = None) -> PublishResult:
        return self.publish_record(self.prepare_record(text, []), record_key or new_record_key())

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

    def get_author_recent_posts(self, actor: str, *, limit: int = 100) -> list[dict]:
        clean = normalize_handle(actor or self.handle)
        if not clean:
            return []
        profile = self.login() if clean == self.handle and self.app_password else self.resolve_profile(clean)
        feed_items, _ = self.fetch_author_feed(profile.did, limit=limit)
        results = []
        for item in feed_items:
            post = _field(item, "post") or {}
            uri = str(_field(post, "uri", ""))
            author = _field(post, "author") or {}
            if _field(item, "reason") is not None or not uri.startswith(f"at://{profile.did}/{POST_COLLECTION}/"):
                continue
            if _field(author, "did", profile.did) != profile.did:
                continue
            record = _field(post, "record") or {}
            text = str(_field(record, "text", "") or "").strip()
            if not text:
                continue
            results.append(
                {
                    "text": text,
                    "uri": uri,
                    "cid": str(_field(post, "cid", "")),
                    "created_at": str(
                        _field(record, "createdAt", "") or _field(record, "created_at", "") or utcnow_iso()
                    ),
                    "rkey": uri.rsplit("/", 1)[-1],
                    "has_embed": bool(_field(record, "embed")),
                    "is_reply": bool(_field(record, "reply")),
                }
            )
        return results


_pool_lock = threading.RLock()
_pool: dict[tuple, BlueskyGateway] = {}


def shared_gateway(db, account_id: int) -> BlueskyGateway:
    import hashlib

    from app.security import protect_secret, unprotect_secret

    account, password = db.get_account_secret(account_id)
    if not account or not password:
        raise BlueskyError("Нужен App Password", auth_error=True)
    fingerprint = hashlib.sha256((account["handle"] + "\0" + password).encode()).hexdigest()
    key = (str(db.path.resolve()), account_id)
    with _pool_lock:
        gateway = _pool.get(key)
        if gateway and getattr(gateway, "fingerprint", "") == fingerprint:
            return gateway
        gateway = BlueskyGateway(account["handle"], password)
        gateway.fingerprint = fingerprint
        stored = db.get_setting(f"session_{account_id}", "")
        try:
            value = json.loads(unprotect_secret(stored)) if stored else {}
            if value.get("fingerprint") == fingerprint:
                gateway.session_string = value["session"]
        except Exception:
            gateway.session_string = ""

        def save(value):
            # Credential tokens must never reach the log.
            try:
                db.set_setting(
                    f"session_{account_id}", protect_secret(json.dumps({"fingerprint": fingerprint, "session": value}))
                )
            except Exception:
                from app.logging_setup import get_logger

                get_logger().warning("Не удалось сохранить сессию аккаунта %s", account_id)

        gateway.session_store = save
        _pool[key] = gateway
        return gateway
