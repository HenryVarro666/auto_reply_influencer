"""instagrapi fallback for Stage-1 sourcing — used only when the login-free path
returns nothing AND credentials are configured.

Self-contained (does not import the agent/ tree). Reads only; never posts.
instagrapi is an OPTIONAL dependency, imported lazily inside the client so this
module imports fine without it installed.

Required env (any one route):
  INSTAGRAPI_SESSIONID                      (preferred — a warmed session cookie)
  INSTAGRAPI_USERNAME + INSTAGRAPI_PASSWORD
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .ig_fetcher import Post

logger = logging.getLogger(__name__)

_SESSION_PATH = Path(__file__).resolve().parent.parent / "data" / "ig_session.json"


def fallback_available() -> bool:
    """True when instagrapi credentials are configured (sessionid, or user+pass)."""
    if os.getenv("INSTAGRAPI_SESSIONID", "").strip():
        return True
    return bool(os.getenv("INSTAGRAPI_USERNAME") and os.getenv("INSTAGRAPI_PASSWORD"))


def _media_to_post(media) -> Post:
    """Convert an instagrapi Media object into our Post."""
    code = getattr(media, "code", "") or ""
    pid = str(getattr(media, "pk", "") or code)
    img = getattr(media, "thumbnail_url", None)
    if not img:
        resources = getattr(media, "resources", None) or []
        if resources:
            img = getattr(resources[0], "thumbnail_url", None)
    user = getattr(media, "user", None)
    owner = getattr(user, "username", None) if user is not None else None
    taken = getattr(media, "taken_at", None)
    return Post(
        post_id=pid,
        url=f"https://www.instagram.com/p/{code}/" if code else "",
        caption=getattr(media, "caption_text", "") or "",
        media_url=str(img) if img else None,
        posted_at=taken.isoformat() if taken else None,
        like_count=getattr(media, "like_count", None),
        owner_handle=owner,
    )


def _dedup(posts: list[Post], limit: int) -> list[Post]:
    if limit <= 0:
        return []
    seen: set[str] = set()
    out: list[Post] = []
    for p in posts:
        if p.post_id in seen:
            continue
        seen.add(p.post_id)
        out.append(p)
        if len(out) >= limit:
            break
    return out


class InstagrapiFallback:
    """Lazy instagrapi wrapper. Build only when fallback_available() is True."""

    def __init__(self):
        self._client = None

    def _get(self):
        if self._client is not None:
            return self._client

        from instagrapi import Client  # type: ignore  # optional dependency

        username = os.getenv("INSTAGRAPI_USERNAME", "")
        password = os.getenv("INSTAGRAPI_PASSWORD", "")
        sessionid = os.getenv("INSTAGRAPI_SESSIONID", "").strip()

        cl = Client()
        if _SESSION_PATH.exists():
            try:
                cl.load_settings(_SESSION_PATH)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not load instagrapi session (%s); fresh login", exc)
        if username and password:
            cl.username = username
            cl.password = password
        try:
            if sessionid:
                cl.login_by_sessionid(sessionid)
            else:
                cl.login(username, password)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "instagrapi login rejected (likely a soft block on an un-warmed "
                f"account). Warm the account, refresh the session, retry. {exc}"
            ) from exc
        try:
            _SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
            cl.dump_settings(_SESSION_PATH)
        except Exception:  # noqa: BLE001
            pass
        self._client = cl
        return cl

    def _with_relogin(self, fn):
        """Run fn(client); on a login_required error, relogin once and retry."""
        cl = self._get()
        try:
            return fn(cl)
        except Exception as exc:  # noqa: BLE001
            if "login_required" in str(exc).lower():
                cl.relogin()
                return fn(cl)
            raise

    def search_hashtag(self, tag: str, limit: int = 100) -> list[Post]:
        clean = tag.lstrip("#").strip()
        medias = self._with_relogin(lambda cl: cl.hashtag_medias_top(clean, amount=limit))
        return [_media_to_post(m) for m in medias]

    def search_keyword(self, query: str, limit: int = 100) -> list[Post]:
        tags = [getattr(h, "name", "") for h in
                self._with_relogin(lambda cl: cl.search_hashtags(query))]
        posts: list[Post] = []
        for tag in [t for t in tags if t][:3]:
            posts.extend(self.search_hashtag(tag, limit))
            if len(posts) >= limit:
                break
        return _dedup(posts, limit)

    def get_single_post(self, url_or_shortcode: str) -> Post | None:
        cl = self._get()
        try:
            if "/" in url_or_shortcode:
                pk = cl.media_pk_from_url(url_or_shortcode)
            else:
                pk = cl.media_pk_from_code(url_or_shortcode)
            return _media_to_post(cl.media_info(pk))
        except Exception as exc:  # noqa: BLE001
            logger.error("instagrapi single-post fetch failed for %s: %s", url_or_shortcode, exc)
            return None

    def get_recent_posts(self, handle: str, limit: int = 12) -> list[Post]:
        def _fetch(cl):
            uid = cl.user_id_from_username(handle)
            return cl.user_medias(uid, amount=limit)
        return [_media_to_post(m) for m in self._with_relogin(_fetch)]
