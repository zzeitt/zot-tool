"""PR 2 tests — multi-signal extraction.

Covers all 6 signal sources independently, plus the ordering/robustness
contract of extract_signals().

These are pure-logic tests (no Zotero API access). They use the `zot` fixture
from conftest.py, which sets dummy env vars before importing zot.
"""

import os
import sys

# Ensure scripts/ is on sys.path so zot_classify can be imported at module level.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import zot_classify
from zot_classify import (
    ArticleInput,
    Signal,
    extract_signals,
    format_signals,
    URL_PATH_PATTERNS,
    SOURCE_WEIGHTS,
)


def _by_source(signals):
    """Group signals by source → {source: Signal}."""
    return {s.source: s for s in signals}


# ---------------------------------------------------------------------------
# Signal 1: user_hint — the most reliable signal
# ---------------------------------------------------------------------------

class TestUserHintSignal:
    def test_hint_produces_signal(self, zot):
        a = ArticleInput(url="", title="x", description="y", user_hints=["#math"])
        sig = _by_source(extract_signals(a)).get("user_hint")
        assert sig is not None
        assert sig.collection_hint == "#math"
        assert sig.confidence == 0.99

    def test_no_hint_no_signal(self, zot):
        a = ArticleInput(url="", title="x", description="y")
        assert "user_hint" not in _by_source(extract_signals(a))

    def test_blank_hints_ignored(self, zot):
        a = ArticleInput(url="", title="x", description="y", user_hints=["", "  "])
        assert "user_hint" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# Signal 2: domain — netloc → known platform
# ---------------------------------------------------------------------------

class TestDomainSignal:
    @pytest.mark.parametrize("url,expected_hint", [
        ("https://news.ycombinator.com/item?id=1", "Misc--hn"),
        ("https://github.com/foo/bar",              "Misc--github"),
        ("https://mp.weixin.qq.com/s/abc",          "Misc--wechat"),
        ("https://arxiv.org/abs/2301.00001",        "Misc--arxiv"),
    ])
    def test_known_domains(self, zot, url, expected_hint):
        a = ArticleInput(url=url, title="t", description="d")
        sig = _by_source(extract_signals(a)).get("domain")
        assert sig is not None, f"no domain signal for {url}"
        assert sig.collection_hint == expected_hint

    def test_unknown_domain_no_signal(self, zot):
        a = ArticleInput(url="https://example.com/post", title="t", description="d")
        assert "domain" not in _by_source(extract_signals(a))

    def test_no_url_no_signal(self, zot):
        a = ArticleInput(url="", title="t", description="d")
        assert "domain" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# Signal 3: url_path — path shape → platform
# ---------------------------------------------------------------------------

class TestUrlPathSignal:
    @pytest.mark.parametrize("url,expected_hint", [
        ("https://arxiv.org/abs/2608.20711",       "Misc--arxiv"),
        ("https://arxiv.org/pdf/2608.20711",       "Misc--arxiv"),
        ("https://www.youtube.com/watch?v=abc",    "Misc--youtube"),
        ("https://news.ycombinator.com/item?id=1", "Misc--hn"),
        ("https://en.wikipedia.org/wiki/Python",    "Misc--wikipedia"),
        ("https://stackoverflow.com/questions/1",  "Misc--stackoverflow"),
        ("https://www.bilibili.com/video/BV1xx",   "Misc--bilibili"),
        ("https://xiaohongshu.com/discovery/item/abc", "Misc--xhs"),
        ("https://zhihu.com/question/123",         "Misc--zhihu"),
    ])
    def test_path_patterns(self, zot, url, expected_hint):
        a = ArticleInput(url=url, title="t", description="d")
        sig = _by_source(extract_signals(a)).get("url_path")
        assert sig is not None, f"no url_path signal for {url}"
        assert sig.collection_hint == expected_hint

    def test_custom_domain_substack_path(self, zot):
        """A custom domain still matches by path shape."""
        a = ArticleInput(url="https://myblog.example.com/p/some-post",
                         title="t", description="d")
        sig = _by_source(extract_signals(a)).get("url_path")
        assert sig is not None
        assert sig.collection_hint == "Misc--substack"

    def test_unmatched_path_no_signal(self, zot):
        a = ArticleInput(url="https://example.com/some/page", title="t", description="d")
        assert "url_path" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# Signal 4: body — needs substantial text
# ---------------------------------------------------------------------------

class TestBodySignal:
    def test_long_body_produces_signal(self, zot):
        body = "This is a substantial article body about formal verification " \
               "and the Lean theorem prover with many more words to pass the threshold."
        a = ArticleInput(url="https://example.com", title="t", description="d",
                         body_text=body)
        sig = _by_source(extract_signals(a)).get("body")
        assert sig is not None
        assert sig.confidence == 0.40

    def test_short_body_no_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="t", description="d",
                         body_text="too short")
        assert "body" not in _by_source(extract_signals(a))

    def test_no_body_no_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="t", description="d")
        assert "body" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# Signal 5: description — short, low weight
# ---------------------------------------------------------------------------

class TestDescriptionSignal:
    def test_description_produces_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="t",
                         description="A sufficiently long meta description here")
        sig = _by_source(extract_signals(a)).get("description")
        assert sig is not None
        assert sig.confidence == 0.20

    def test_short_description_no_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="t", description="short")
        assert "description" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# Signal 6: title — always present (if not a URL)
# ---------------------------------------------------------------------------

class TestTitleSignal:
    def test_title_produces_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="Fermat Last Theorem",
                         description="d")
        sig = _by_source(extract_signals(a)).get("title")
        assert sig is not None
        assert sig.confidence == 0.15

    def test_url_title_no_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="https://example.com",
                         description="d")
        assert "title" not in _by_source(extract_signals(a))

    def test_empty_title_no_signal(self, zot):
        a = ArticleInput(url="https://example.com", title="", description="d")
        assert "title" not in _by_source(extract_signals(a))


# ---------------------------------------------------------------------------
# extract_signals contract
# ---------------------------------------------------------------------------

class TestExtractSignalsContract:
    def test_ordered_by_source_weight(self, zot):
        """Signals come back in descending SOURCE_WEIGHTS order."""
        a = ArticleInput(
            url="https://news.ycombinator.com/item?id=1",
            title="Some Title Here",
            description="A description long enough to trigger the signal",
            body_text="A body long enough to trigger the signal as well, " * 3,
            user_hints=["#x"],
        )
        signals = extract_signals(a)
        weights = [SOURCE_WEIGHTS[s.source] for s in signals]
        assert weights == sorted(weights, reverse=True)

    def test_never_raises_on_garbage(self, zot):
        """Malformed input must not raise."""
        for a in [
            ArticleInput(url="not a url", title=None, description=None),
            ArticleInput(url="://///", title="", description=""),
            ArticleInput(url="x" * 5000, title="y" * 5000, description="z" * 5000),
        ]:
            signals = extract_signals(a)  # must not raise
            assert isinstance(signals, list)

    def test_all_signals_are_frozen(self, zot):
        a = ArticleInput(url="https://example.com", title="Title Here",
                         description="desc")
        for sig in extract_signals(a):
            assert isinstance(sig, Signal)
            with pytest.raises(Exception):
                sig.confidence = 0.5

    def test_independent_extractors(self, zot):
        """One source failing doesn't kill the others."""
        # Only a URL — title/desc/body are empty, so only domain/url_path fire
        a = ArticleInput(url="https://news.ycombinator.com/item?id=1",
                         title="", description="")
        signals = extract_signals(a)
        sources = {s.source for s in signals}
        assert "domain" in sources
        assert "url_path" in sources


# ---------------------------------------------------------------------------
# format_signals
# ---------------------------------------------------------------------------

class TestFormatSignals:
    def test_empty(self, zot):
        assert "no signals" in format_signals([])

    def test_renders_each_signal(self, zot):
        a = ArticleInput(url="https://github.com/foo/bar", title="Title",
                         description="desc")
        out = format_signals(extract_signals(a))
        assert "domain" in out
        assert "conf=" in out


# ---------------------------------------------------------------------------
# URL_PATH_PATTERNS table sanity
# ---------------------------------------------------------------------------

class TestUrlPathPatternsTable:
    def test_all_patterns_have_hint_and_confidence(self, zot):
        for pattern, hint, conf in URL_PATH_PATTERNS:
            assert isinstance(pattern, str) and pattern
            assert isinstance(hint, str) and hint
            assert 0.0 < conf <= 1.0

    def test_hints_are_bare_sub_names(self, zot):
        """Table stores bare sub-names; the Misc-- prefix is added by the code."""
        for _pattern, hint, _conf in URL_PATH_PATTERNS:
            assert not hint.startswith("Misc--"), (
                "table must store bare names — the code prepends 'Misc--'"
            )
            assert "-" not in hint or hint.startswith("Misc--")
