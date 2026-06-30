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
