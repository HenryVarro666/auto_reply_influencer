import httpx
import pytest

from core import ig_fetcher
from core.ig_fetcher import (
    FetchError,
    InstagramFetcher,
    Post,
    ProfileNotFound,
    _merge_dedup_posts,
    _parse_embed_post,
    _parse_hashtag_posts,
    _parse_topsearch,
    shortcode_from_url,
)


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


# --- Task 2 tests ---


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
    a = [Post(post_id="1", url="u1"), Post(post_id="2", url="u2")]
    b = [Post(post_id="2", url="u2"), Post(post_id="3", url="u3")]
    out = _merge_dedup_posts([a, b], limit=10)
    assert [p.post_id for p in out] == ["1", "2", "3"]
    assert [p.post_id for p in _merge_dedup_posts([a, b], limit=2)] == ["1", "2"]


def test_merge_dedup_posts_zero_limit_returns_empty():
    posts = [Post(post_id="1", url="u1"), Post(post_id="2", url="u2")]
    assert _merge_dedup_posts([posts], limit=0) == []


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


# --- Task 3 tests ---


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


# --- Task 4 tests ---


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
    assert p.caption == "hello world"


def test_parse_embed_post_raises_when_empty():
    with pytest.raises(FetchError):
        _parse_embed_post("<html>nothing useful</html>", "SHORT3")
