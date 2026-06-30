# Keyword / Hashtag / Account / Post Fetch Sources — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Stage-1 fetch source posts from a single account, a single post URL, a hashtag, or a general keyword (default top 100) — not only the CSV account list — with a login-free-first / instagrapi-fallback strategy.

**Architecture:** Only the Stage-1 sourcing layer changes. `core/ig_fetcher.py` (login-free, httpx + `x-ig-app-id`) gains hashtag/keyword/single-post readers; a new self-contained `core/ig_fallback.py` wraps `instagrapi` for when login-free returns nothing. `run.py` selects the source from CLI flags, runs login-free-then-fallback, and feeds posts into the existing dedup/download/insert/generate/tasks pipeline unchanged. Pure parsers and orchestration helpers are unit-tested; network and 3rd-party calls are not.

**Tech Stack:** Python 3.13 (code targets 3.8+ via `from __future__ import annotations`), httpx[http2], SQLite (stdlib), pytest 9, optional instagrapi.

## Global Constraints

- Every new `.py` module starts with `from __future__ import annotations` (matches existing modules; keeps `X | None` annotations valid on 3.8+).
- Login-free path is primary; instagrapi is fallback only, gated on credentials being present. **Never auto-post/auto-comment** — instagrapi is used for reads only.
- No new *required* dependencies. `instagrapi` is added **commented-out** in `requirements.txt` (same convention as `openai`/`gemini`).
- HTTP/2 is mandatory for IG web endpoints — reuse the existing `httpx.Client(http2=True, headers={"x-ig-app-id": ...})` pattern; never swap in `requests`/`urllib`.
- Default post count for hashtag/keyword search = **100**, read from `config.yaml` key `default_top` (and mirrored in `run._DEFAULTS`).
- The 4 source flags (`--account`, `--post`, `--hashtag`, `--keyword`) are mutually exclusive (argparse group). No flag → CSV (current behavior).
- Time window: account/CSV keep the "last N hours" filter (default 2); hashtag/keyword/post apply **no** time filter unless `--hours` is explicitly passed.
- CLI user-facing messages stay bilingual in the existing Chinese style.
- Run tests from the project root with `python -m pytest` (puts the repo root on `sys.path` so `import core` / `import run` work).

---

### Task 1: `Post.owner_handle` field + `shortcode_from_url` helper

**Files:**
- Modify: `core/ig_fetcher.py` (the `Post` dataclass ~line 49; add a module function near `normalize_handle` ~line 64)
- Create: `tests/test_ig_fetcher_helpers.py`
- Create: `conftest.py` (empty — ensures repo root import path)

**Interfaces:**
- Produces: `Post(..., owner_handle: str | None = None)` — every post now carries the author handle (None when the caller already knows it, e.g. per-account fetch). `shortcode_from_url(value: str) -> str | None`.

- [ ] **Step 1: Create empty root conftest so `import core`/`import run` resolve**

Create `conftest.py` at the repo root with a single comment line:

```python
# Present so pytest adds the repo root to sys.path (lets tests import core/ and run.py).
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_ig_fetcher_helpers.py`:

```python
from core.ig_fetcher import Post, shortcode_from_url


def test_post_owner_handle_defaults_to_none():
    p = Post(post_id="1", url="https://x")
    assert p.owner_handle is None


def test_shortcode_from_url_variants():
    assert shortcode_from_url("https://www.instagram.com/p/ABC-123_/") == "ABC-123_"
    assert shortcode_from_url("https://instagram.com/reel/XYZ789/?hl=en") == "XYZ789"
    assert shortcode_from_url("https://www.instagram.com/tv/TV_42/") == "TV_42"
    assert shortcode_from_url("ABC123") == "ABC123"
    assert shortcode_from_url("") is None
    assert shortcode_from_url("https://example.com/foo/bar") is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: FAIL — `ImportError: cannot import name 'shortcode_from_url'`.

- [ ] **Step 4: Add the `owner_handle` field**

In `core/ig_fetcher.py`, in the `Post` dataclass, add the field after `comments_disabled`:

```python
    comments_disabled: bool | None = None
    owner_handle: str | None = None  # author of THIS post (set for hashtag/keyword/single-post results)
```

- [ ] **Step 5: Add the `shortcode_from_url` helper**

In `core/ig_fetcher.py`, add after `normalize_handle(...)`:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: PASS (3 tests).

- [ ] **Step 7: Commit**

```bash
git add conftest.py tests/test_ig_fetcher_helpers.py core/ig_fetcher.py
git commit -m "feat(fetch): add Post.owner_handle and shortcode_from_url helper"
```

---

### Task 2: Login-free hashtag search (`search_hashtag`) + shared request helpers

**Files:**
- Modify: `core/ig_fetcher.py` (add URL consts, `_client_kwargs`, `_with_retries`, refactor `get_recent_posts`, add `_media_node_to_post`, `_first_image_url`, `_merge_dedup_posts`, `_parse_hashtag_posts`, `InstagramFetcher.search_hashtag`)
- Modify: `tests/test_ig_fetcher_helpers.py` (append cases)

**Interfaces:**
- Consumes: `Post`, `_ts_to_iso` (existing).
- Produces:
  - `_client_kwargs(self) -> dict`
  - `_with_retries(self, fn: Callable[[], T], label: str) -> T`
  - `_merge_dedup_posts(groups: list[list[Post]], limit: int) -> list[Post]`
  - `_parse_hashtag_posts(payload: dict, limit: int) -> list[Post]`
  - `InstagramFetcher.search_hashtag(self, tag: str, limit: int = 100) -> list[Post]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ig_fetcher_helpers.py`:

```python
import httpx
import pytest

from core import ig_fetcher
from core.ig_fetcher import (
    FetchError,
    InstagramFetcher,
    ProfileNotFound,
    _merge_dedup_posts,
    _parse_hashtag_posts,
)


def test_client_kwargs_proxy_and_http2(monkeypatch):
    monkeypatch.delenv("IG_PROXY", raising=False)
    assert "proxy" not in InstagramFetcher(None)._client_kwargs()
    assert InstagramFetcher(None)._client_kwargs()["http2"] is True
    assert InstagramFetcher("http://gw:1")._client_kwargs()["proxy"] == "http://gw:1"


def test_with_retries_returns_on_success(monkeypatch):
    monkeypatch.delenv("IG_PROXY", raising=False)
    assert InstagramFetcher(None, retries=2)._with_retries(lambda: "ok", "x") == "ok"


def test_with_retries_raises_after_attempts(monkeypatch):
    monkeypatch.delenv("IG_PROXY", raising=False)
    monkeypatch.setattr(ig_fetcher.time, "sleep", lambda *_: None)
    f = InstagramFetcher(None, retries=1)
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(FetchError):
        f._with_retries(boom, "x")
    assert calls["n"] == 2  # retries + 1


def test_with_retries_propagates_profile_not_found(monkeypatch):
    monkeypatch.delenv("IG_PROXY", raising=False)
    def nf():
        raise ProfileNotFound("404")
    with pytest.raises(ProfileNotFound):
        InstagramFetcher(None, retries=3)._with_retries(nf, "x")


def test_merge_dedup_posts_dedups_and_caps():
    from core.ig_fetcher import Post
    a = [Post(post_id="1", url="u1"), Post(post_id="2", url="u2")]
    b = [Post(post_id="2", url="u2"), Post(post_id="3", url="u3")]
    out = _merge_dedup_posts([a, b], limit=10)
    assert [p.post_id for p in out] == ["1", "2", "3"]
    assert [p.post_id for p in _merge_dedup_posts([a, b], limit=2)] == ["1", "2"]


def _hashtag_payload():
    media = {
        "pk": "111", "code": "SC1",
        "caption": {"text": "great match"},
        "image_versions2": {"candidates": [{"url": "https://cdn/img1.jpg"}]},
        "user": {"username": "fan_account"},
        "taken_at": 1700000000, "like_count": 9, "comment_count": 3,
    }
    section = {"layout_content": {"medias": [{"media": media}]}}
    return {"data": {"top": {"sections": [section]}, "recent": {"sections": []}}}


def test_parse_hashtag_posts_extracts_fields():
    posts = _parse_hashtag_posts(_hashtag_payload(), limit=50)
    assert len(posts) == 1
    p = posts[0]
    assert p.post_id == "111"
    assert p.url == "https://www.instagram.com/p/SC1/"
    assert p.caption == "great match"
    assert p.media_url == "https://cdn/img1.jpg"
    assert p.owner_handle == "fan_account"
    assert p.like_count == 9


def test_parse_hashtag_posts_handles_carousel_and_empty():
    carousel_media = {
        "pk": "222", "code": "SC2", "caption": None,
        "carousel_media": [{"image_versions2": {"candidates": [{"url": "https://cdn/c.jpg"}]}}],
        "user": {"username": "u2"},
    }
    payload = {"data": {"top": {"sections": [
        {"layout_content": {"medias": [{"media": carousel_media}]}}
    ]}}}
    posts = _parse_hashtag_posts(payload, limit=50)
    assert posts[0].media_url == "https://cdn/c.jpg"
    assert posts[0].caption == ""
    assert _parse_hashtag_posts({}, limit=50) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: FAIL — `ImportError` for `_merge_dedup_posts` / `_parse_hashtag_posts`.

- [ ] **Step 3: Add imports + URL constants**

In `core/ig_fetcher.py`, update the imports near the top:

```python
import datetime as _dt
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

import httpx
```

Add below the existing `_WEB_PROFILE_URL` line:

```python
from urllib.parse import quote

_HASHTAG_URL = "https://www.instagram.com/api/v1/tags/web_info/?tag_name={}"
_TOPSEARCH_URL = "https://www.instagram.com/web/search/topsearch/?context=blended&query={}"
_EMBED_URL = "https://www.instagram.com/p/{}/embed/captioned/"

_T = TypeVar("_T")
```

- [ ] **Step 4: Add the pure parse/merge helpers (module level)**

Add after `_parse_posts(...)` in `core/ig_fetcher.py`:

```python
def _first_image_url(media: dict) -> str | None:
    def from_candidates(m: dict) -> str | None:
        cands = ((m.get("image_versions2") or {}).get("candidates")) or []
        return cands[0].get("url") if cands else None

    url = from_candidates(media)
    if url:
        return url
    carousel = media.get("carousel_media") or []
    return from_candidates(carousel[0]) if carousel else None


def _media_node_to_post(media: dict) -> Post | None:
    if not media:
        return None
    shortcode = media.get("code") or ""
    pid = str(media.get("pk") or media.get("id") or shortcode)
    if not pid:
        return None
    cap = media.get("caption")
    caption = (cap.get("text") if isinstance(cap, dict) else "") or ""
    user = media.get("user") or {}
    return Post(
        post_id=pid,
        url=f"https://www.instagram.com/p/{shortcode}/" if shortcode else "",
        caption=caption,
        media_url=_first_image_url(media),
        posted_at=_ts_to_iso(media.get("taken_at")),
        like_count=media.get("like_count"),
        comment_count=media.get("comment_count"),
        owner_handle=user.get("username"),
    )


def _merge_dedup_posts(groups: list[list[Post]], limit: int) -> list[Post]:
    seen: set[str] = set()
    out: list[Post] = []
    for group in groups:
        for p in group:
            if p.post_id in seen:
                continue
            seen.add(p.post_id)
            out.append(p)
            if len(out) >= limit:
                return out
    return out


def _parse_hashtag_posts(payload: dict, limit: int) -> list[Post]:
    data = payload.get("data") or {}
    posts: list[Post] = []
    for key in ("top", "recent"):
        for sec in (data.get(key) or {}).get("sections") or []:
            for item in ((sec.get("layout_content") or {}).get("medias")) or []:
                p = _media_node_to_post(item.get("media") or {})
                if p:
                    posts.append(p)
    return _merge_dedup_posts([posts], limit)
```

- [ ] **Step 5: Add `_client_kwargs` + `_with_retries`, and refactor `get_recent_posts`**

In `core/ig_fetcher.py`, inside `InstagramFetcher`, add these two methods (place above `_request_once`):

```python
    def _client_kwargs(self) -> dict:
        kwargs: dict = {
            "http2": True,  # mandatory — see module docstring
            "headers": {"User-Agent": _UA, "x-ig-app-id": _IG_APP_ID},
            "timeout": self.timeout,
            "follow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return kwargs

    def _with_retries(self, fn: Callable[[], _T], label: str) -> _T:
        attempts = self.retries + 1
        last_err: Exception | None = None
        for i in range(attempts):
            try:
                return fn()
            except ProfileNotFound:
                raise
            except httpx.HTTPStatusError as exc:
                last_err = exc
                if exc.response.status_code not in _RETRYABLE_HTTP:
                    break
            except httpx.HTTPError as exc:
                last_err = exc
            except Exception as exc:  # noqa: BLE001 — parse / empty payload
                last_err = exc
                break
            if not self.proxy and i < attempts - 1:
                time.sleep(2 ** i)
        raise FetchError(f"{label}: {last_err}")
```

Replace the body of `_request_once` to use `_client_kwargs` (keep its parsing logic):

```python
    def _request_once(self, handle: str, limit: int) -> list[Post]:
        with httpx.Client(**self._client_kwargs()) as client:
            resp = client.get(_WEB_PROFILE_URL.format(handle))
        if resp.status_code == 404:
            raise ProfileNotFound(f"@{handle}: not found (404)")
        resp.raise_for_status()
        data = resp.json()
        user = (data.get("data") or {}).get("user")
        if user is None:
            raise FetchError(f"@{handle}: empty profile payload (private/restricted?)")
        return _parse_posts(user, limit)
```

Replace `get_recent_posts` to delegate to `_with_retries`:

```python
    def get_recent_posts(self, handle: str, limit: int = 12) -> list[Post]:
        """Return the newest ``limit`` posts for ``handle`` (newest first)."""
        return self._with_retries(lambda: self._request_once(handle, limit), f"@{handle}")
```

- [ ] **Step 6: Add `_fetch_json` + `search_hashtag`**

Add to `InstagramFetcher` (below `get_recent_posts`):

```python
    def _fetch_json(self, url: str) -> dict:
        def _once() -> dict:
            with httpx.Client(**self._client_kwargs()) as client:
                resp = client.get(url)
            if resp.status_code == 404:
                raise ProfileNotFound(url)
            resp.raise_for_status()
            return resp.json()

        return self._with_retries(_once, url)

    def search_hashtag(self, tag: str, limit: int = 100) -> list[Post]:
        """Login-free hashtag search via tags/web_info. Returns up to ``limit`` posts."""
        clean = tag.lstrip("#").strip()
        payload = self._fetch_json(_HASHTAG_URL.format(quote(clean)))
        return _parse_hashtag_posts(payload, limit)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: PASS (all helper/parse/retry tests).

- [ ] **Step 8: Commit**

```bash
git add core/ig_fetcher.py tests/test_ig_fetcher_helpers.py
git commit -m "feat(fetch): login-free hashtag search + shared request/retry helpers"
```

---

### Task 3: Login-free keyword search (`search_keyword`) + topsearch parsing

**Files:**
- Modify: `core/ig_fetcher.py` (add `_parse_topsearch`, `InstagramFetcher.search_keyword`)
- Modify: `tests/test_ig_fetcher_helpers.py` (append cases)

**Interfaces:**
- Consumes: `_merge_dedup_posts`, `search_hashtag`, `get_recent_posts`, `_fetch_json`.
- Produces:
  - `_parse_topsearch(payload: dict) -> tuple[list[str], list[str]]` — (hashtag names, usernames), ranked.
  - `InstagramFetcher.search_keyword(self, query: str, limit: int = 100) -> list[Post]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ig_fetcher_helpers.py`:

```python
from core.ig_fetcher import Post, _parse_topsearch


def test_parse_topsearch_ranked_tags_and_users():
    payload = {
        "hashtags": [
            {"hashtag": {"name": "football"}},
            {"hashtag": {"name": "soccer"}},
        ],
        "users": [{"user": {"username": "leomessi"}}],
        "places": [{"place": {"title": "ignored"}}],
    }
    tags, users = _parse_topsearch(payload)
    assert tags == ["football", "soccer"]
    assert users == ["leomessi"]
    assert _parse_topsearch({}) == ([], [])


def test_search_keyword_uses_hashtags_first(monkeypatch):
    f = InstagramFetcher(None)
    monkeypatch.setattr(f, "_fetch_json", lambda url: {
        "hashtags": [{"hashtag": {"name": "football"}}],
        "users": [{"user": {"username": "leomessi"}}],
    })
    monkeypatch.setattr(f, "search_hashtag",
                        lambda tag, limit: [Post(post_id="h1", url="u", owner_handle="a")])
    # users only used to top up; here the hashtag already satisfies limit
    monkeypatch.setattr(f, "get_recent_posts",
                        lambda handle, limit=12: [Post(post_id="u1", url="u", owner_handle=handle)])
    out = f.search_keyword("football", limit=1)
    assert [p.post_id for p in out] == ["h1"]


def test_search_keyword_tops_up_from_users(monkeypatch):
    f = InstagramFetcher(None)
    monkeypatch.setattr(f, "_fetch_json", lambda url: {
        "hashtags": [{"hashtag": {"name": "football"}}],
        "users": [{"user": {"username": "leomessi"}}],
    })
    monkeypatch.setattr(f, "search_hashtag",
                        lambda tag, limit: [Post(post_id="h1", url="u", owner_handle="a")])
    monkeypatch.setattr(f, "get_recent_posts",
                        lambda handle, limit=12: [Post(post_id="u1", url="u", owner_handle=handle)])
    out = f.search_keyword("football", limit=5)
    assert [p.post_id for p in out] == ["h1", "u1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -k topsearch -v`
Expected: FAIL — `ImportError: cannot import name '_parse_topsearch'`.

- [ ] **Step 3: Add `_parse_topsearch` (module level)**

Add after `_parse_hashtag_posts` in `core/ig_fetcher.py`:

```python
def _parse_topsearch(payload: dict) -> tuple[list[str], list[str]]:
    tags = [
        name for h in (payload.get("hashtags") or [])
        if (name := (h.get("hashtag") or {}).get("name"))
    ]
    users = [
        uname for u in (payload.get("users") or [])
        if (uname := (u.get("user") or {}).get("username"))
    ]
    return tags, users
```

- [ ] **Step 4: Add `search_keyword`**

Add to `InstagramFetcher` (below `search_hashtag`):

```python
    def search_keyword(self, query: str, limit: int = 100) -> list[Post]:
        """Login-free general keyword search.

        topsearch resolves the query to ranked hashtags + accounts (it does NOT
        return posts directly); we pull posts from the top hashtags first, then
        top up from the top accounts' recent posts. Cross-source dedup + cap.
        """
        payload = self._fetch_json(_TOPSEARCH_URL.format(quote(query)))
        tags, users = _parse_topsearch(payload)
        groups: list[list[Post]] = []
        for tag in tags[:3]:
            try:
                groups.append(self.search_hashtag(tag, limit))
            except FetchError:
                continue
            if sum(len(g) for g in groups) >= limit:
                return _merge_dedup_posts(groups, limit)
        for user in users[:5]:
            if sum(len(g) for g in groups) >= limit:
                break
            try:
                groups.append(self.get_recent_posts(user, limit=12))
            except (FetchError, ProfileNotFound):
                continue
        return _merge_dedup_posts(groups, limit)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: PASS (topsearch + keyword tests included).

- [ ] **Step 6: Commit**

```bash
git add core/ig_fetcher.py tests/test_ig_fetcher_helpers.py
git commit -m "feat(fetch): login-free keyword search via topsearch -> hashtags + accounts"
```

---

### Task 4: Login-free single post (`get_single_post`) via embed page

**Files:**
- Modify: `core/ig_fetcher.py` (add embed regexes, `_json_unescape`, `_parse_embed_post`, `_fetch_text`, `InstagramFetcher.get_single_post`)
- Modify: `tests/test_ig_fetcher_helpers.py` (append cases)

**Interfaces:**
- Consumes: `shortcode_from_url`, `_with_retries`, `_client_kwargs`.
- Produces:
  - `_parse_embed_post(html: str, shortcode: str) -> Post`
  - `InstagramFetcher.get_single_post(self, url_or_shortcode: str) -> Post`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ig_fetcher_helpers.py`:

```python
from core.ig_fetcher import _parse_embed_post


def test_parse_embed_post_from_json_blob():
    html = (
        'prefix '
        '"display_url":"https:\\/\\/cdn.example\\/img.jpg?x=1\\u00261",'
        '"edge_media_to_caption":{"edges":[{"node":{"text":"Vamos! \\u26bd"}}]},'
        '"username":"leomessi" suffix'
    )
    p = _parse_embed_post(html, "SHORT1")
    assert p.post_id == "SHORT1"
    assert p.url == "https://www.instagram.com/p/SHORT1/"
    assert p.media_url == "https://cdn.example/img.jpg?x=1&1"
    assert p.caption == "Vamos! ⚽"
    assert p.owner_handle == "leomessi"


def test_parse_embed_post_html_fallback():
    html = (
        '<img class="EmbeddedMediaImage" src="https://cdn/h.jpg"/>'
        '<a class="UsernameText">html_user</a>'
        '<div class="Caption">hello world</div>'
    )
    p = _parse_embed_post(html, "SHORT2")
    assert p.media_url == "https://cdn/h.jpg"
    assert p.owner_handle == "html_user"


def test_parse_embed_post_raises_when_empty():
    with pytest.raises(FetchError):
        _parse_embed_post("<html>nothing useful</html>", "SHORT3")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -k embed -v`
Expected: FAIL — `ImportError: cannot import name '_parse_embed_post'`.

- [ ] **Step 3: Add embed regexes + `_json_unescape` + `_parse_embed_post`**

Add after `_parse_topsearch` in `core/ig_fetcher.py`:

```python
_EMBED_DISPLAY_RE = re.compile(r'"display_url":"(.*?)"')
_EMBED_USER_JSON_RE = re.compile(r'"username":"(.*?)"')
_EMBED_CAPTION_JSON_RE = re.compile(
    r'"edge_media_to_caption":\{"edges":\[\{"node":\{"text":"(.*?)"\}\}\]'
)
_EMBED_IMG_HTML_RE = re.compile(r'class="EmbeddedMediaImage"[^>]*?src="(.*?)"')
_EMBED_USER_HTML_RE = re.compile(r'class="UsernameText"[^>]*>(.*?)<')
_EMBED_CAPTION_HTML_RE = re.compile(r'class="Caption"[^>]*>(.*?)</div>', re.DOTALL)


def _json_unescape(s: str) -> str:
    """Decode a JSON string body (handles \\/, \\uXXXX, \\" etc.)."""
    try:
        return json.loads('"' + s + '"')
    except Exception:  # noqa: BLE001
        return s


def _parse_embed_post(html: str, shortcode: str) -> Post:
    if not html:
        raise FetchError(f"empty embed for {shortcode}")

    img = None
    m = _EMBED_DISPLAY_RE.search(html)
    if m:
        img = _json_unescape(m.group(1))
    if not img:
        m = _EMBED_IMG_HTML_RE.search(html)
        if m:
            img = _json_unescape(m.group(1))

    caption = ""
    m = _EMBED_CAPTION_JSON_RE.search(html)
    if m:
        caption = _json_unescape(m.group(1))
    elif (m := _EMBED_CAPTION_HTML_RE.search(html)):
        caption = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    owner = None
    m = _EMBED_USER_JSON_RE.search(html)
    if m:
        owner = _json_unescape(m.group(1))
    if not owner:
        m = _EMBED_USER_HTML_RE.search(html)
        if m:
            owner = m.group(1).strip()

    if not img and not caption:
        raise FetchError(f"could not parse embed for {shortcode}")
    return Post(
        post_id=shortcode,
        url=f"https://www.instagram.com/p/{shortcode}/",
        caption=caption,
        media_url=img,
        owner_handle=owner,
    )
```

- [ ] **Step 4: Add `_fetch_text` + `get_single_post`**

Add to `InstagramFetcher` (below `search_keyword`):

```python
    def _fetch_text(self, url: str) -> str:
        def _once() -> str:
            with httpx.Client(**self._client_kwargs()) as client:
                resp = client.get(url)
            if resp.status_code == 404:
                raise ProfileNotFound(url)
            resp.raise_for_status()
            return resp.text

        return self._with_retries(_once, url)

    def get_single_post(self, url_or_shortcode: str) -> Post:
        """Login-free single-post read via the public embed page."""
        sc = shortcode_from_url(url_or_shortcode)
        if not sc:
            raise FetchError(f"could not parse shortcode from {url_or_shortcode!r}")
        html = self._fetch_text(_EMBED_URL.format(sc))
        return _parse_embed_post(html, sc)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_ig_fetcher_helpers.py -v`
Expected: PASS (all ig_fetcher tests).

- [ ] **Step 6: Commit**

```bash
git add core/ig_fetcher.py tests/test_ig_fetcher_helpers.py
git commit -m "feat(fetch): login-free single-post read via embed page"
```

---

### Task 5: instagrapi fallback module (`core/ig_fallback.py`)

**Files:**
- Create: `core/ig_fallback.py`
- Create: `tests/test_ig_fallback.py`

**Interfaces:**
- Consumes: `Post`, `shortcode_from_url` from `core.ig_fetcher`.
- Produces:
  - `fallback_available() -> bool`
  - `_media_to_post(media) -> Post` (converts an instagrapi Media-like object)
  - `class InstagrapiFallback` with `search_hashtag(tag, limit=100) -> list[Post]`, `search_keyword(query, limit=100) -> list[Post]`, `get_single_post(url) -> Post | None`, `get_recent_posts(handle, limit=12) -> list[Post]`.

Note: the live instagrapi client calls are **not** unit-tested (network + 3rd-party, optional dep). Only `fallback_available()` and `_media_to_post()` are.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ig_fallback.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace

from core import ig_fallback
from core.ig_fallback import _media_to_post, fallback_available


def test_fallback_available_checks_env(monkeypatch):
    monkeypatch.delenv("INSTAGRAPI_SESSIONID", raising=False)
    monkeypatch.delenv("INSTAGRAPI_USERNAME", raising=False)
    monkeypatch.delenv("INSTAGRAPI_PASSWORD", raising=False)
    assert fallback_available() is False

    monkeypatch.setenv("INSTAGRAPI_SESSIONID", "abc")
    assert fallback_available() is True
    monkeypatch.delenv("INSTAGRAPI_SESSIONID")

    monkeypatch.setenv("INSTAGRAPI_USERNAME", "u")
    assert fallback_available() is False  # password still missing
    monkeypatch.setenv("INSTAGRAPI_PASSWORD", "p")
    assert fallback_available() is True


def test_media_to_post_maps_fields():
    media = SimpleNamespace(
        pk=999, code="CODE9", caption_text="hi there",
        thumbnail_url="https://cdn/t.jpg", resources=[],
        user=SimpleNamespace(username="owner9"),
        taken_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        like_count=12,
    )
    p = _media_to_post(media)
    assert p.post_id == "999"
    assert p.url == "https://www.instagram.com/p/CODE9/"
    assert p.caption == "hi there"
    assert p.media_url == "https://cdn/t.jpg"
    assert p.owner_handle == "owner9"
    assert p.posted_at == "2026-01-01T00:00:00+00:00"
    assert p.like_count == 12


def test_media_to_post_falls_back_to_resource_thumbnail():
    media = SimpleNamespace(
        pk=1, code="C", caption_text="", thumbnail_url=None,
        resources=[SimpleNamespace(thumbnail_url="https://cdn/r.jpg")],
        user=None, taken_at=None, like_count=None,
    )
    p = _media_to_post(media)
    assert p.media_url == "https://cdn/r.jpg"
    assert p.owner_handle is None
    assert p.posted_at is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ig_fallback.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.ig_fallback'`.

- [ ] **Step 3: Create `core/ig_fallback.py`**

```python
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

from .ig_fetcher import Post, shortcode_from_url

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

    def search_hashtag(self, tag: str, limit: int = 100) -> list[Post]:
        cl = self._get()
        clean = tag.lstrip("#").strip()
        try:
            medias = cl.hashtag_medias_top(clean, amount=limit)
        except Exception as exc:  # noqa: BLE001
            if "login_required" in str(exc).lower():
                cl.relogin()
                medias = cl.hashtag_medias_top(clean, amount=limit)
            else:
                raise
        return [_media_to_post(m) for m in medias]

    def search_keyword(self, query: str, limit: int = 100) -> list[Post]:
        cl = self._get()
        tags = [getattr(h, "name", "") for h in cl.search_hashtags(query)]
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
                sc = shortcode_from_url(url_or_shortcode) or url_or_shortcode
                pk = cl.media_pk_from_code(sc)
            return _media_to_post(cl.media_info(pk))
        except Exception as exc:  # noqa: BLE001
            logger.error("instagrapi single-post fetch failed for %s: %s", url_or_shortcode, exc)
            return None

    def get_recent_posts(self, handle: str, limit: int = 12) -> list[Post]:
        cl = self._get()
        uid = cl.user_id_from_username(handle)
        return [_media_to_post(m) for m in cl.user_medias(uid, amount=limit)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_ig_fallback.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add core/ig_fallback.py tests/test_ig_fallback.py
git commit -m "feat(fetch): add self-contained instagrapi fallback (reads only)"
```

---

### Task 6: run.py orchestration helpers (source select, ingest, fallback gather)

**Files:**
- Modify: `run.py` (add `Source` dataclass, `select_source`, `_account_for_post`, `_ingest_posts`, `_gather_with_fallback`; add `default_top` to `_DEFAULTS`)
- Create: `tests/test_run_helpers.py`

**Interfaces:**
- Consumes: `store`, `_within_window`, `Post`.
- Produces:
  - `Source(kind: str, value: str | None = None)`
  - `select_source(args) -> Source`  (kind ∈ {csv, account, post, hashtag, keyword})
  - `_account_for_post(post: Post) -> dict`
  - `_ingest_posts(conn, items: list[tuple[Post, dict]], *, media_dir: Path, date: str, proxy: str | None, cutoff) -> tuple[int, int]`  (returns (new, existing))
  - `_gather_with_fallback(primary: Callable[[], list], fallback, *, label: str) -> list`

- [ ] **Step 1: Write the failing test**

Create `tests/test_run_helpers.py`:

```python
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import run
from core import store
from core.ig_fetcher import Post


def _args(**over):
    base = dict(account=None, post=None, hashtag=None, keyword=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_select_source_defaults_to_csv():
    assert run.select_source(_args()).kind == "csv"


def test_select_source_picks_each_flag():
    assert run.select_source(_args(account="messi")).kind == "account"
    assert run.select_source(_args(post="https://ig/p/x/")).kind == "post"
    assert run.select_source(_args(hashtag="football")).kind == "hashtag"
    s = run.select_source(_args(keyword="world cup"))
    assert (s.kind, s.value) == ("keyword", "world cup")


def test_account_for_post_uses_owner_handle():
    acc = run._account_for_post(Post(post_id="1", url="u", owner_handle="leomessi"))
    assert acc["handle"] == "leomessi"
    assert acc["url"] == "https://www.instagram.com/leomessi/"
    assert acc["type"] == ""
    fallback = run._account_for_post(Post(post_id="2", url="u", owner_handle=None))
    assert fallback["handle"] == "unknown"


def test_ingest_posts_inserts_then_dedups(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "download_media", lambda *a, **k: True)
    conn = store.connect(tmp_path / "t.db")
    p = Post(post_id="1", url="u", media_url="http://img", owner_handle="messi")
    items = [(p, run._account_for_post(p))]
    assert run._ingest_posts(conn, items, media_dir=tmp_path / "m",
                             date="2026-06-29", proxy=None, cutoff=None) == (1, 0)
    assert run._ingest_posts(conn, items, media_dir=tmp_path / "m",
                             date="2026-06-29", proxy=None, cutoff=None) == (0, 1)


def test_ingest_posts_applies_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "download_media", lambda *a, **k: True)
    conn = store.connect(tmp_path / "t.db")
    old = Post(post_id="9", url="u", media_url="x",
               posted_at="2000-01-01T00:00:00+00:00", owner_handle="a")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    new, existing = run._ingest_posts(conn, [(old, run._account_for_post(old))],
                                      media_dir=tmp_path / "m", date="2026-06-29",
                                      proxy=None, cutoff=cutoff)
    assert (new, existing) == (0, 0)  # filtered out by the time window


def test_gather_with_fallback_paths():
    called = {"fb": 0}
    def fb_ok():
        called["fb"] += 1
        return [Post(post_id="fb", url="u")]

    # primary has results -> fallback not called
    out = run._gather_with_fallback(lambda: [Post(post_id="p", url="u")], fb_ok, label="x")
    assert [p.post_id for p in out] == ["p"] and called["fb"] == 0

    # primary empty -> fallback used
    out = run._gather_with_fallback(lambda: [], fb_ok, label="x")
    assert [p.post_id for p in out] == ["fb"]

    # primary raises -> fallback used
    def boom():
        raise RuntimeError("login_required")
    out = run._gather_with_fallback(boom, fb_ok, label="x")
    assert [p.post_id for p in out] == ["fb"]

    # primary empty, no fallback -> empty
    assert run._gather_with_fallback(lambda: [], None, label="x") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_helpers.py -v`
Expected: FAIL — `AttributeError: module 'run' has no attribute 'select_source'`.

- [ ] **Step 3: Add `default_top` to `_DEFAULTS`**

In `run.py`, add to the `_DEFAULTS` dict (after `"max_delay_seconds"` or near the fetch keys):

```python
    "default_top": 100,
```

- [ ] **Step 4: Add the helpers**

In `run.py`, add imports near the top (after existing imports):

```python
from dataclasses import dataclass
from typing import Callable

from core.ig_fetcher import Post
```

Add the `Source` dataclass + `select_source` (place after `load_config`/`_abs` helpers):

```python
@dataclass
class Source:
    kind: str                 # csv | account | post | hashtag | keyword
    value: str | None = None  # handle / url / tag / query


def select_source(args) -> Source:
    """Map CLI args to a Source. Mutual exclusivity is enforced by argparse;
    if no source flag is set we default to the CSV account list."""
    if getattr(args, "account", None):
        return Source("account", args.account)
    if getattr(args, "post", None):
        return Source("post", args.post)
    if getattr(args, "hashtag", None):
        return Source("hashtag", args.hashtag)
    if getattr(args, "keyword", None):
        return Source("keyword", args.keyword)
    return Source("csv")
```

Add `_account_for_post`, `_ingest_posts`, `_gather_with_fallback` (place near `read_accounts`/`_within_window`):

```python
def _account_for_post(post: Post) -> dict:
    """Synthesize an account dict for a post whose owner came from search results."""
    handle = post.owner_handle or "unknown"
    url = f"https://www.instagram.com/{handle}/" if handle != "unknown" else ""
    return {"handle": handle, "name": "", "type": "", "country_league": "",
            "followers": None, "url": url}


def _ingest_posts(conn, items, *, media_dir, date, proxy, cutoff) -> tuple[int, int]:
    """Download + insert each (Post, account) pair. Returns (new, existing).

    cutoff=None means no time filter (hashtag/keyword/post); a datetime applies
    the 'published within the window' filter (account/CSV).
    """
    new = existing = 0
    for post, account in items:
        if cutoff is not None and not _within_window(post.posted_at, cutoff):
            continue
        if store.post_exists(conn, post.post_id):
            existing += 1
            continue
        handle = account["handle"]
        media_path = media_dir / date / handle / f"{post.post_id}.jpg"
        ok = store.download_media(post.media_url, media_path, proxy=proxy)
        store.insert_post(conn, post, account, str(media_path) if ok else None)
        new += 1
    return new, existing


def _gather_with_fallback(primary: Callable[[], list], fallback, *, label: str) -> list:
    """Run primary(); on error or empty result fall back to fallback() if provided.

    primary/fallback are zero-arg callables returning list[Post]; fallback may be None.
    """
    try:
        posts = primary() or []
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ {label}: 免登录失败 — {exc}")
        posts = []
    if posts:
        return posts
    if fallback is None:
        print(f"  ℹ {label}: 免登录无结果，且未配置 instagrapi 兜底凭据，跳过。")
        return []
    print(f"  ↪ {label}: 免登录无结果，改用 instagrapi 兜底…")
    try:
        return fallback() or []
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {label}: instagrapi 兜底也失败 — {exc}")
        return []
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_run_helpers.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add run.py tests/test_run_helpers.py
git commit -m "feat(fetch): add source-select, ingest, and fallback-gather helpers"
```

---

### Task 7: Wire the new sources into the `fetch`/`run` CLI

**Files:**
- Modify: `run.py` (`_add_source_args`, rebuild `cmd_fetch` into account vs search dispatch, refactor account loop to use `_ingest_posts`, extend `build_parser`)
- Modify: `config.yaml` (add `default_top: 100`)
- Modify: `tests/test_run_helpers.py` (append argparse exclusivity tests)

**Interfaces:**
- Consumes: everything from Tasks 1–6, plus `core.ig_fallback`.
- Produces: `fetch`/`run` accept `--account/--post/--hashtag/--keyword` (mutually exclusive) and `--top`. `cmd_fetch_accounts(...)` and `cmd_fetch_search(...)` internal handlers.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_run_helpers.py`:

```python
import pytest


def test_argparse_rejects_two_sources():
    p = run.build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["fetch", "--hashtag", "x", "--account", "y"])


def test_argparse_accepts_single_source_and_top():
    p = run.build_parser()
    ns = p.parse_args(["fetch", "--keyword", "world cup", "--top", "30"])
    assert run.select_source(ns).value == "world cup"
    assert ns.top == 30


def test_run_subcommand_also_has_sources():
    p = run.build_parser()
    ns = p.parse_args(["run", "--hashtag", "football"])
    assert run.select_source(ns).kind == "hashtag"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_helpers.py -k argparse -v`
Expected: FAIL — `error: unrecognized arguments: --hashtag` (SystemExit but not from the mutual-exclusion path) → the asserts fail.

- [ ] **Step 3: Add `_add_source_args` and update `build_parser`**

In `run.py`, add the helper above `build_parser`:

```python
def _add_source_args(parser) -> None:
    """Attach the mutually-exclusive Stage-1 source flags + --top to a subparser."""
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--account", default=None,
                     help="single account handle/URL (no CSV needed)")
    grp.add_argument("--post", default=None,
                     help="single post URL or shortcode")
    grp.add_argument("--hashtag", default=None,
                     help="search a hashtag across accounts (default top 100)")
    grp.add_argument("--keyword", default=None,
                     help="general keyword search across accounts (default top 100)")
    parser.add_argument("--top", type=int, default=None,
                        help="max posts for hashtag/keyword search (default config.default_top=100)")
```

In `build_parser`, call it for both `fetch` and `run`. Update those two blocks:

```python
    f = sub.add_parser("fetch", help="Stage 1: fetch recent posts")
    f.add_argument("--hours", type=float, default=None, help="time window (default from config)")
    f.add_argument("--limit", type=int, default=None, help="only first N accounts (CSV source)")
    f.add_argument("--csv", default=None, help="override CSV path")
    _add_source_args(f)
    f.set_defaults(func=cmd_fetch)
```

```python
    r = sub.add_parser("run", help="fetch + generate + tasks")
    r.add_argument("--hours", type=float, default=None)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--csv", default=None)
    r.add_argument("--provider", default=None)
    r.add_argument("--max-tasks-per-day", type=int, default=None, dest="max_tasks_per_day")
    _add_source_args(r)
    r.set_defaults(func=cmd_run)
```

- [ ] **Step 4: Add the `core.ig_fallback` import**

In `run.py`, update the core import line:

```python
from core import comment_generator, ig_fallback, store, task_writer
```

- [ ] **Step 5: Replace `cmd_fetch` with a source dispatcher + two handlers**

Replace the entire existing `cmd_fetch` function with:

```python
def cmd_fetch(args, cfg) -> None:
    proxy = os.getenv("IG_PROXY") or None
    if not proxy:
        print("ℹ️  未设置 IG_PROXY，默认使用本地 IP 直连。"
              "（账号较多时本地 IP 可能被限流；要稳可在 .env 配置代理。）")
    media_dir = _abs(cfg["media_dir"])
    conn = store.connect(_abs(cfg["db_path"]))
    fetcher = InstagramFetcher(proxy, retries=int(cfg["fetch_retries"]))
    date = store.today_str()
    source = select_source(args)

    if source.kind in ("csv", "account"):
        _cmd_fetch_accounts(args, cfg, conn, fetcher, proxy, media_dir, date, source)
    else:
        _cmd_fetch_search(args, cfg, conn, fetcher, proxy, media_dir, date, source)


def _cmd_fetch_accounts(args, cfg, conn, fetcher, proxy, media_dir, date, source) -> None:
    hours = args.hours if args.hours is not None else cfg["time_window_hours"]
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)

    if source.kind == "account":
        handle = normalize_handle(source.value)
        if not handle:
            print(f"无法解析账号：{source.value!r}")
            return
        accounts = [{"handle": handle, "name": "", "type": "", "country_league": "",
                     "followers": None, "url": f"https://www.instagram.com/{handle}/"}]
    else:
        accounts = read_accounts(_abs(args.csv or cfg["csv_path"]), args.limit)

    new_posts = existing = errors = 0
    print(f"① 抓帖：{len(accounts)} 个账号 · 窗口=过去 {hours}h · "
          f"出口={'代理 IP' if proxy else '本地 IP 直连'}")
    bar = _make_bar(len(accounts), "① 抓帖", "账号")
    for i, acc in enumerate(accounts, 1):
        handle = acc["handle"]
        posts = None
        try:
            posts = fetcher.get_recent_posts(handle, limit=int(cfg["posts_per_account"]))
        except ProfileNotFound:
            errors += 1
            _emit(bar, f"  ✗ @{handle}: 账号不存在 (404)")
        except FetchError as exc:
            errors += 1
            _emit(bar, f"  ✗ @{handle}: 抓取失败 — {exc}")

        if posts is not None:
            n, e = _ingest_posts(conn, [(p, acc) for p in posts], media_dir=media_dir,
                                 date=date, proxy=proxy, cutoff=cutoff)
            new_posts += n
            existing += e
            if n:
                _emit(bar, f"  ✓ @{handle}: 新增 {n}")
        _advance(bar, f"@{handle} 新{new_posts} 旧{existing} 错{errors}")
        if i < len(accounts):
            time.sleep(random.uniform(float(cfg["min_delay_seconds"]),
                                      float(cfg["max_delay_seconds"])))
    _close(bar)
    print(f"✅ 抓帖完成：新增 {new_posts} 帖，已存在 {existing}，账号错误 {errors}。")


def _cmd_fetch_search(args, cfg, conn, fetcher, proxy, media_dir, date, source) -> None:
    top = args.top if getattr(args, "top", None) else int(cfg["default_top"])
    cutoff = None
    if args.hours is not None:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=args.hours)
    fb = ig_fallback.InstagrapiFallback() if ig_fallback.fallback_available() else None

    if source.kind == "hashtag":
        label = f"标签 #{source.value.lstrip('#')}"
        primary = lambda: fetcher.search_hashtag(source.value, top)
        fallback = (lambda: fb.search_hashtag(source.value, top)) if fb else None
    elif source.kind == "keyword":
        label = f"关键词 “{source.value}”"
        primary = lambda: fetcher.search_keyword(source.value, top)
        fallback = (lambda: fb.search_keyword(source.value, top)) if fb else None
    else:  # post
        label = f"单帖 {source.value}"
        primary = lambda: [fetcher.get_single_post(source.value)]
        fallback = (lambda: [p for p in [fb.get_single_post(source.value)] if p]) if fb else None

    window_note = "" if cutoff is None else f" · 仅过去 {args.hours}h"
    print(f"① 抓帖（{label}）· 取前 {top} · "
          f"出口={'代理 IP' if proxy else '本地 IP 直连'}{window_note}")
    posts = _gather_with_fallback(primary, fallback, label=label)
    items = [(p, _account_for_post(p)) for p in posts]
    new, existing = _ingest_posts(conn, items, media_dir=media_dir, date=date,
                                  proxy=proxy, cutoff=cutoff)
    print(f"✅ 抓帖完成：{label} 命中 {len(posts)} 帖，新增 {new}，已存在 {existing}。")
```

- [ ] **Step 6: Add `default_top` to `config.yaml`**

In `config.yaml`, under the Stage-1 section, add:

```yaml
default_top: 100            # hashtag/keyword search: how many posts to pull (--top overrides)
```

- [ ] **Step 7: Run the full test suite**

Run: `python -m pytest -v`
Expected: PASS (all tests across the 3 test files).

- [ ] **Step 8: Verify the CLI help shows the new flags**

Run: `python run.py fetch -h`
Expected: output lists `--account`, `--post`, `--hashtag`, `--keyword`, `--top`.

Run: `python run.py fetch --hashtag x --account y`
Expected: argparse error `argument --account: not allowed with argument --hashtag` (exit code 2).

- [ ] **Step 9: Commit**

```bash
git add run.py config.yaml tests/test_run_helpers.py
git commit -m "feat(cli): add --account/--post/--hashtag/--keyword/--top to fetch & run"
```

---

### Task 8: Docs + optional dependency wiring

**Files:**
- Modify: `.env.example` (instagrapi vars)
- Modify: `requirements.txt` (commented instagrapi line)
- Modify: `README.md` (new flags + two-layer strategy + caveats)

**Interfaces:** None (docs/config only).

- [ ] **Step 1: Add instagrapi env vars to `.env.example`**

Append to `.env.example`:

```bash
# --- instagrapi fallback (OPTIONAL) ------------------------------------------
# Only used when login-free hashtag/keyword/post search returns nothing.
# Reads only — never auto-posts. Prefer SESSIONID from a warmed account.
# INSTAGRAPI_SESSIONID=
# INSTAGRAPI_USERNAME=
# INSTAGRAPI_PASSWORD=
```

- [ ] **Step 2: Add the commented instagrapi dependency**

In `requirements.txt`, under the Stage-2 optional block, add:

```python
# instagrapi>=2.0       # OPTIONAL fallback for --hashtag/--keyword/--post when the login-free path is blocked
```

- [ ] **Step 3: Document the new sources in `README.md`**

In `README.md`, in the "使用方法（命令）" section, add after the existing `fetch` examples:

````markdown
# 新增：换数据源抓帖（默认免登录；失败时若配了 instagrapi 凭据则自动兜底）
python run.py fetch --account messi                    # 单个账号，免 CSV
python run.py fetch --post https://www.instagram.com/p/ABC123/   # 单条帖子链接
python run.py fetch --hashtag football                # 标签搜索，默认前 100
python run.py fetch --keyword "world cup" --top 50    # 通用关键词搜索，取前 50
python run.py run --hashtag football                  # run 也支持，一步出任务单
````

And add these rows to the parameter table (`### 各参数说明`):

````markdown
| `--account <handle>` | 只抓单个指定账号（免 CSV/txt） | 无 |
| `--post <url>` | 只抓单条帖子（URL 或 shortcode） | 无 |
| `--hashtag <tag>` | 按话题标签跨账号搜索 | 无 |
| `--keyword <q>` | 按通用关键词跨账号搜索 | 无 |
| `--top N` | 标签/关键词搜索取多少帖 | `config.yaml` 的 `default_top`（100） |
````

Add a short subsection after the parameter table:

````markdown
### 数据源与两层抓取策略

- **不传任何数据源参数** → 沿用现状，读 CSV 账号清单。
- 四个数据源参数 `--account/--post/--hashtag/--keyword` **互斥**，一次只能用一个。
- **时间窗口**：`--account`/CSV 仍按 `--hours`(默认 2h) 只留新帖；`--hashtag`/`--keyword`/`--post`
  默认不按时间过滤（取「前 N 热门/相关帖」），显式传 `--hours` 才会再叠加时间筛选。
- **两层抓取**：先走**免登录**公开接口（最低封号风险）；标签/关键词/单帖在免登录拿不到时，
  若 `.env` 配了 instagrapi 凭据（`INSTAGRAPI_SESSIONID` 或 `USERNAME`+`PASSWORD`），则自动用
  instagrapi **兜底读取**（仅读取、绝不自动发评论）；没配凭据就如实跳过。
- **足球语气提醒**：评论提示词是「足球 → 大学申请」定向，关键词搜到非足球内容时类比会牵强；
  非足球关键词请自行调整 `prompts/`。
- ⚠️ instagrapi 是非官方私有 API，有封号风险，仅建议低频使用、用可弃用的小号。
````

- [ ] **Step 4: Verify the full suite still passes (no code changed, sanity check)**

Run: `python -m pytest -q`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add .env.example requirements.txt README.md
git commit -m "docs: document new fetch sources + optional instagrapi fallback"
```

---

## Self-Review

**1. Spec coverage**

| Spec item | Task |
|---|---|
| §2 `--account` single account, no file | Task 7 (`_cmd_fetch_accounts`) + Task 6 (`select_source`) |
| §2 `--post` single post link | Task 4 (`get_single_post`) + Task 7 (`_cmd_fetch_search`) |
| §2 `--hashtag` | Task 2 (`search_hashtag`) + Task 7 |
| §2 `--keyword` | Task 3 (`search_keyword`) + Task 7 |
| §2 `--top` default 100, mutual exclusivity | Task 7 (`_add_source_args`, mutually-exclusive group) + Task 6/7 (`default_top`) |
| §3.1 login-free hashtag/keyword/single-post | Tasks 2, 3, 4 |
| §3.1 `Post.owner_handle` | Task 1 |
| §3.2 instagrapi fallback (self-contained, gated, reads only) | Task 5 + Task 7 wiring |
| §4A time-window rules | Task 6 (`_ingest_posts` cutoff) + Task 7 (cutoff per source) |
| §4B keyword resolution (topsearch → tags + accounts) | Task 3 |
| §4C football tone (no prompt change) | Task 8 (documented; no code) |
| §5 downstream unchanged; synth account dict; dedup | Task 6 (`_account_for_post`, `_ingest_posts`) — relies on existing `store`/`comment_generator` |
| §5 config/.env/requirements/README | Tasks 7, 8 |

No uncovered spec requirements.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to Task N". Every code step shows full code; every test step shows real assertions.

**3. Type consistency:** `Post.owner_handle` (Task 1) is read by `_media_node_to_post`/`_parse_embed_post` (Tasks 2,4), `_media_to_post` (Task 5), `_account_for_post` (Task 6). `_merge_dedup_posts(groups, limit)` defined Task 2, reused Task 3. `search_hashtag(tag, limit=100)` / `search_keyword(query, limit=100)` / `get_single_post(url_or_shortcode)` signatures match between `InstagramFetcher` (Tasks 2–4) and `InstagrapiFallback` (Task 5) and their call sites (Task 7). `_ingest_posts(conn, items, *, media_dir, date, proxy, cutoff) -> (int,int)` defined Task 6, called in both handlers Task 7. `select_source(args).kind/value` consistent across Tasks 6–7. `default_top` added to both `_DEFAULTS` (Task 6) and `config.yaml` (Task 7). Consistent.
