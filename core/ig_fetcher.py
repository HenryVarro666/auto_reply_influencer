"""Instagram post fetcher — Stage 1.

Self-contained port of the proven pattern in
``follow_matrix/monitor/platforms/instagram/fetcher.py``: it reads a public
profile's recent posts from Instagram's ``web_profile_info`` endpoint over
HTTP/2, WITHOUT login. The lightest, least ban-prone way to read posts.

Key facts (do not "simplify" these away):
  - HTTP/2 is mandatory. Instagram returns 429 to HTTP/1.1 requests on this
    endpoint, so a plain ``requests``/``urllib`` client is reliably blocked no
    matter the exit IP. We use ``httpx`` with ``http2=True``.
  - The ``x-ig-app-id`` header is what makes the public endpoint return JSON.
  - Set ``IG_PROXY`` to a rotating residential gateway. A FRESH ``httpx.Client``
    per request opens a new connection, so the gateway hands out a new exit IP
    each call — this is the main lever for staying under per-IP rate limits.
    On a retryable error we just request again (a new IP); with no proxy we back
    off 1s/2s/4s on the single IP instead.

This module is read-only. It never logs in and never writes to Instagram.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import time
from dataclasses import dataclass

import httpx

_WEB_PROFILE_URL = "https://i.instagram.com/api/v1/users/web_profile_info/?username={}"
_IG_APP_ID = "936619743392459"
_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
_RETRYABLE_HTTP = {401, 429, 500, 502, 503, 504}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]+$")


class FetchError(Exception):
    """Generic fetch failure (transport, parse, empty payload)."""


class ProfileNotFound(FetchError):
    """Profile does not exist (HTTP 404) — never retried."""


@dataclass
class Post:
    """A normalized Instagram post (subset we care about for commenting)."""

    post_id: str
    url: str
    caption: str = ""
    media_url: str | None = None
    posted_at: str | None = None  # ISO8601 UTC string
    like_count: int | None = None
    comment_count: int | None = None
    view_count: int | None = None
    comments_disabled: bool | None = None
    owner_handle: str | None = None  # author of THIS post (set for hashtag/keyword/single-post results)


def normalize_handle(raw: str) -> str | None:
    """Extract a bare IG username from a profile URL or @handle.

    Port of ``monitor/loader.py:normalize_handle`` (instagram only). Returns
    None if nothing usable can be parsed.
    """
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    value = re.sub(r"^https?://", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(www\.)?instagram\.com/", "", value, flags=re.IGNORECASE)
    value = value.lstrip("@")
    value = value.split("?")[0].split("#")[0].split("/")[0].strip()
    if not value or not _USERNAME_RE.match(value):
        return None
    return value


_SHORTCODE_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)")


def shortcode_from_url(value: str) -> str | None:
    """Extract an IG post shortcode from a /p//reel//tv/ URL, or accept a bare shortcode."""
    if not value:
        return None
    v = value.strip()
    m = _SHORTCODE_RE.search(v)
    if m:
        return m.group(1)
    if "/" not in v and re.fullmatch(r"[A-Za-z0-9_-]+", v):
        return v
    return None


def _ts_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat()


def _parse_posts(user: dict, limit: int) -> list[Post]:
    """Build Post objects from a ``web_profile_info`` user dict (newest first).

    Field mapping mirrors ``fetcher.py:_parse_posts`` exactly.
    """
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges") or []
    posts: list[Post] = []
    for edge in edges[:limit]:
        n = edge.get("node") or {}
        cap_edges = (n.get("edge_media_to_caption") or {}).get("edges") or []
        caption = cap_edges[0]["node"]["text"] if cap_edges else ""
        shortcode = n.get("shortcode") or ""
        likes = (n.get("edge_media_preview_like") or n.get("edge_liked_by") or {}).get("count")
        comments = (n.get("edge_media_to_comment") or {}).get("count")
        views = n.get("video_view_count") if n.get("is_video") else None
        posts.append(
            Post(
                post_id=str(n.get("id") or shortcode),
                url=f"https://www.instagram.com/p/{shortcode}/",
                caption=caption,
                media_url=n.get("display_url"),
                posted_at=_ts_to_iso(n.get("taken_at_timestamp")),
                like_count=likes,
                comment_count=comments,
                view_count=views,
                comments_disabled=n.get("comments_disabled"),
            )
        )
    return posts


class InstagramFetcher:
    """Fetches recent posts for public profiles via the web endpoint."""

    def __init__(self, proxy: str | None = None, *, retries: int = 5, timeout: float = 20.0):
        # Gateway mode: one rotating-gateway URL reused for every request; the
        # gateway rotates the exit IP. Falls back to IG_PROXY from the env.
        self.proxy = proxy if proxy is not None else (os.getenv("IG_PROXY") or None)
        self.retries = max(0, retries)
        self.timeout = timeout

    def _request_once(self, handle: str, limit: int) -> list[Post]:
        kwargs: dict = {
            "http2": True,  # mandatory — see module docstring
            "headers": {"User-Agent": _UA, "x-ig-app-id": _IG_APP_ID},
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        # Fresh client per call => new connection => new gateway exit IP.
        with httpx.Client(**kwargs) as client:
            resp = client.get(_WEB_PROFILE_URL.format(handle))
        if resp.status_code == 404:
            raise ProfileNotFound(f"@{handle}: not found (404)")
        resp.raise_for_status()
        data = resp.json()
        user = (data.get("data") or {}).get("user")
        if user is None:
            raise FetchError(f"@{handle}: empty profile payload (private/restricted?)")
        return _parse_posts(user, limit)

    def get_recent_posts(self, handle: str, limit: int = 12) -> list[Post]:
        """Return the newest ``limit`` posts for ``handle`` (newest first).

        Raises ProfileNotFound on 404, FetchError on anything else after
        exhausting retries.
        """
        attempts = self.retries + 1
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                return self._request_once(handle, limit)
            except ProfileNotFound:
                raise  # never retried
            except httpx.HTTPStatusError as exc:
                last_err = exc
                if exc.response.status_code not in _RETRYABLE_HTTP:
                    break
            except httpx.HTTPError as exc:
                last_err = exc  # transient transport/timeout — retry
            except Exception as exc:  # noqa: BLE001 — parse / empty payload
                last_err = exc
                break
            # With a rotating gateway, retry immediately on a fresh IP. With no
            # proxy there is one IP, so back off 1s/2s/4s.
            if not self.proxy and i < attempts - 1:
                time.sleep(2 ** i)
        raise FetchError(f"@{handle}: {last_err}")
