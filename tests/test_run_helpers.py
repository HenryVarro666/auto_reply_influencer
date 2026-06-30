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
