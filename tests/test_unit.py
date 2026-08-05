"""Pure-logic unit tests — no Zotero API access needed.

Tests functions that do string processing, URL detection, emoji mapping,
concept extraction, etc. Fast and safe to run without network.
"""

import pytest


# We import zot as a module to access its pure functions.
# These tests don't need any API fixtures — just the module.
@pytest.fixture(scope="module")
def zot(zot_mod):
    """Re-export zot_mod as 'zot' for cleaner test code."""
    return zot_mod


class TestDomainSubcollName:
    """_domain_subcoll_name() — URL → short name mapping."""

    @pytest.mark.parametrize("url,expected", [
        ("https://mp.weixin.qq.com/s/SZv3pDXPrL9vwV3Ua_84Kg", "wechat"),
        ("https://weixin.qq.com/abc", "wechat"),
        ("https://github.com/zzeitt/zot-tool", "github"),
        ("https://arxiv.org/abs/2301.00001", "arxiv"),
        ("https://news.ycombinator.com/item?id=12345", "hn"),
        ("https://ycombinator.com/item?id=12345", "hn"),
        ("https://www.bilibili.com/video/BV1xx", "bilibili"),
        ("https://zhihu.com/question/123", "zhihu"),
        ("https://xiaohongshu.com/discovery/item/abc", "xhs"),
        ("https://juejin.cn/post/123", "juejin"),
        ("https://stackoverflow.com/questions/123", "stackoverflow"),
        ("https://medium.com/@user/post-123", "medium"),
        ("https://substack.com/@user/post-123", "substack"),
        ("https://www.youtube.com/watch?v=abc", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("https://podcasts.apple.com/podcast/id123", "podcast"),
        ("https://open.spotify.com/episode/abc", "spotify"),
        ("https://en.wikipedia.org/wiki/Python", "wikipedia"),
        ("https://chaspark.com/post/123", "chaspark"),
    ])
    def test_known_domains(self, zot, url, expected):
        """Known domains map to correct short names."""
        assert zot._domain_subcoll_name(url) == expected

    def test_unknown_domain(self, zot):
        """Unknown domain returns None."""
        assert zot._domain_subcoll_name("https://example.com/post/123") is None

    def test_empty_url(self, zot):
        """Empty/None URL returns None."""
        assert zot._domain_subcoll_name("") is None
        assert zot._domain_subcoll_name(None) is None


class TestWechatUrlDetection:
    """_is_wechat_url() detection."""

    def test_wechat_url_positive(self, zot):
        assert zot._is_wechat_url("https://mp.weixin.qq.com/s/SZv3pDXPrL9vwV3Ua_84Kg")

    def test_wechat_url_negative(self, zot):
        assert not zot._is_wechat_url("https://example.com/article")
        assert not zot._is_wechat_url("https://weixin.qq.com")  # main site, not mp


class TestIsUrl:
    """_is_url() detection."""

    def test_http_url(self, zot):
        assert zot._is_url("https://example.com")
        assert zot._is_url("http://example.com")

    def test_not_url(self, zot):
        assert not zot._is_url("Just a title")
        assert not zot._is_url("example.com")  # no scheme


class TestEmojiForTag:
    """_emoji_for_tag() mapping."""

    @pytest.mark.parametrize("text,expected", [
        # _emoji_for_tag checks keyword substring match in order
        ("llm", "🤖"),
        ("claude", "🤖"),
        ("gpt", "🤖"),
        ("大模型", "🤖"),
        ("economics", "💰"),
        ("finance", "💰"),
        ("投资", "💰"),
        ("programming", "💻"),
        ("python", "💻"),
        ("mathematics", "🔢"),
        ("math", "🔢"),
        ("philosophy", "🤔"),
        ("ethics", "🤔"),
        ("podcast", "🎙️"),
        ("video", "📺"),
        ("tutorial", "📚"),
        ("how to", "📚"),
        ("paper", "📄"),
        ("health", "🏥"),
        ("security", "🔒"),
        ("plugin", "🛠️"),
    ])
    def test_emoji_mapping(self, zot, text, expected):
        assert zot._emoji_for_tag(text) == expected

    def test_unknown_fallback(self, zot):
        """Unknown text gets 🔗 fallback."""
        assert zot._emoji_for_tag("xyzzy123_unknown_topic") == "🔗"


class TestExtractConcepts:
    """_extract_concepts() — concept extraction from text."""

    def test_ai_concept(self, zot):
        concepts = zot._extract_concepts(
            "Deep Learning with Neural Networks and Machine Learning")
        assert any("AI-ML" in c for c in concepts), \
            f"Expected AI-ML tag, got: {concepts}"

    def test_programming_concept(self, zot):
        concepts = zot._extract_concepts(
            "Python Programming Guide for Software Developers")
        assert any("编程" in c for c in concepts), \
            f"Expected 编程 tag, got: {concepts}"

    def test_url_text_returns_empty(self, zot):
        """URL-only text returns empty list (can't extract meaningful concepts)."""
        concepts = zot._extract_concepts("https://example.com/some-article")
        assert concepts == []

    def test_fallback_keyword(self, zot):
        """Text without matching concepts gets a fallback tag based on first word."""
        concepts = zot._extract_concepts(
            "Zimbabwe wildlife conservation efforts")
        # Should get a fallback tag from first meaningful word
        assert len(concepts) <= 1
        if concepts:
            assert concepts[0].startswith("#")


class TestFallbackSubName:
    """_fallback_sub_name_from_url() and _fallback_sub_name_from_title()."""

    def test_url_fallback(self, zot):
        assert zot._fallback_sub_name_from_url(
            "https://example.com/post/123") == "example"
        assert zot._fallback_sub_name_from_url(
            "https://sub.domain.co.uk/path") == "sub"

    def test_title_fallback(self, zot):
        name = zot._fallback_sub_name_from_title("Machine Learning Guide 2024")
        assert "machine" in name.lower() or "learning" in name.lower()

    def test_title_stop_words_filtered(self, zot):
        """Stop words like 'the', 'a', 'in' are filtered from title fallback."""
        name = zot._fallback_sub_name_from_title(
            "The Art of Programming")
        # "the" and "of" are stop words, "art" and "programming" should remain
        assert "the" not in name.lower()
        assert "of" not in name.lower()


class TestDetectBinaryUrl:
    """_detect_binary_url() detection."""

    def test_pdf_detection(self, zot):
        is_bin, ct, hint = zot._detect_binary_url(
            "https://example.com/paper.pdf")
        assert is_bin
        assert ct == "application/pdf"
        assert hint == "paper.pdf"

    def test_epub_detection(self, zot):
        is_bin, ct, hint = zot._detect_binary_url(
            "https://example.com/book.epub")
        assert is_bin
        assert ct == "application/epub+zip"

    def test_html_not_binary(self, zot):
        is_bin, ct, hint = zot._detect_binary_url(
            "https://example.com/article.html")
        assert not is_bin

    def test_libgen_detection(self, zot):
        is_bin, ct, hint = zot._detect_binary_url(
            "https://libgen.li/get.php?md5=abc123")
        assert is_bin
        assert ct == "application/pdf"


class TestExtForContentType:
    """_ext_for_content_type() mapping."""

    def test_known_types(self, zot):
        assert zot._ext_for_content_type("application/pdf") == "pdf"
        assert zot._ext_for_content_type("application/epub+zip") == "epub"
        assert zot._ext_for_content_type("application/zip") == "zip"
        assert zot._ext_for_content_type("application/msword") == "doc"

    def test_unknown_type(self, zot):
        assert zot._ext_for_content_type("application/x-unknown") == "bin"


class TestNoPlatformSystemCall:
    """Regression: IS_WINDOWS must NOT use platform.system().

    platform.system() spawns a subprocess (ver / WMI) on Windows and hangs
    on Python 3.14. Use sys.platform ("win32"|"linux"|"darwin") instead.
    """

    def test_no_platform_import(self):
        """zot.py must not import platform at module level."""
        import os
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        src = open(os.path.join(scripts_dir, "zot.py"), encoding="utf-8").read()
        # Allow platform in comments/strings only
        lines = [l for l in src.split("\n")
                 if not l.strip().startswith("#") and "platform" in l]
        for line in lines:
            assert "import platform" not in line, (
                f"zot.py imports 'platform' — use sys.platform instead:\n  {line.strip()}"
            )

    def test_is_windows_uses_sys_platform(self):
        """IS_WINDOWS must derive from sys.platform, not platform.system()."""
        import os, re
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        src = open(os.path.join(scripts_dir, "zot.py"), encoding="utf-8").read()
        # Check for the correct pattern
        assert "sys.platform" in src, (
            "IS_WINDOWS should use sys.platform (not platform.system())"
        )
        # platform.system() should never appear
        assert "platform.system()" not in src, (
            "platform.system() found in zot.py — hangs on Python 3.14/Windows"
        )
