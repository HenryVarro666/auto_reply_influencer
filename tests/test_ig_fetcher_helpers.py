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
