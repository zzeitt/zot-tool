"""Pure-logic unit tests — no Zotero API access needed.

Tests functions that do string processing, URL detection, emoji mapping,
concept extraction, etc. Fast and safe to run without network.
"""

import pytest


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

    @pytest.mark.parametrize("url,expected", [
        # HN: news.* subdomains + the parent domain
        ("https://news.ycombinator.com/item?id=12345", "hn"),
        ("https://ycombinator.com/item?id=12345", "hn"),
        # GitHub: gist, api, raw subdomains
        ("https://gist.github.com/user/abc123", "github"),
        ("https://api.github.com/repos/foo/bar", "github"),
        # YouTube: m, music subdomains
        ("https://m.youtube.com/watch?v=abc", "youtube"),
        ("https://music.youtube.com/watch?v=abc", "youtube"),
        # Wikipedia: en, zh subdomains
        ("https://en.wikipedia.org/wiki/Python", "wikipedia"),
        ("https://zh.wikipedia.org/wiki/Python", "wikipedia"),
        # WeChat: only mp. + main (not subdomain of each other)
        ("https://mp.weixin.qq.com/s/abc", "wechat"),
        ("https://weixin.qq.com/abc", "wechat"),
        # Bilibili: www subdomain
        ("https://www.bilibili.com/video/BV1xx", "bilibili"),
        # Stack Overflow: subdomain
        ("https://subdomain.stackoverflow.com/q/123", "stackoverflow"),
    ])
    def test_subdomain_matching(self, zot, url, expected):
        """Subdomains should match the parent domain in DOMAIN_TO_SUBCOLL."""
        assert zot._domain_subcoll_name(url) == expected

    @pytest.mark.parametrize("url", [
        # substring 假阳性场景（修复前会误匹配）
        "https://notgithub.com/evil",           # 不 endswith .github.com
        "https://evil-github.com/x",            # 域名里含 "github.com" 但不是子域
        "https://github.com.attacker.com/x",    # endswith .attacker.com 不 .github.com
        "https://notwikipedia.org/page",        # substring 包含 wikipedia.org
        "https://fakearxiv.org/abs/1234",       # 包含 arxiv.org 但不是 .arxiv.org
        "https://badstackoverflow.com/q",       # 包含 stackoverflow.com 但不 .stackoverflow.com
    ])
    def test_substring_false_positives(self, zot, url):
        """substring 假阳性场景必须返回 None（旧实现会误匹配）."""
        assert zot._domain_subcoll_name(url) is None, \
            f"Should not match: {url}"

    def test_length_preference_more_specific_first(self, zot):
        """More specific (longer) domain entries win on ties."""
        # mp.weixin.qq.com (16 chars) vs weixin.qq.com (13 chars)
        # mp.weixin.qq.com 是更具体的条目，应优先匹配
        assert zot._domain_subcoll_name(
            "https://mp.weixin.qq.com/s/abc") == "wechat"
        # weixin.qq.com 自己单独匹配
        assert zot._domain_subcoll_name(
            "https://weixin.qq.com/abc") == "wechat"


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


def _synthetic_vocab():
    """Synthetic vocabulary fixture — **never** real library data.

    Shape mirrors what ``_parse_vocab`` produces for a real library, scaled
    down to a handful of invented tags (``/demo📦``, ``#demo-alpha``, …).
    """
    return {
        "roots": [
            {"tag": "/demo📦", "slug": "demo", "n": 40, "types": [0],
             "type": 0, "status": False, "children_count": 3},
            {"tag": "/widget🔧", "slug": "widget", "n": 12, "types": [1],
             "type": 1, "status": False, "children_count": 1},
            {"tag": "/unread", "slug": "unread", "n": 99, "types": [0, 1],
             "type": 0, "status": True, "children_count": 0},
        ],
        "children": {
            "demo": [
                {"tag": "#demo-alpha", "slug": "demo-alpha", "child": "alpha",
                 "norm": "alpha", "n": 30, "types": [0], "type": 0},
                {"tag": "#demo-阿尔法", "slug": "demo-阿尔法", "child": "阿尔法",
                 "norm": "阿尔法", "n": 25, "types": [0], "type": 0},
                {"tag": "#demo-cv", "slug": "demo-cv", "child": "cv",
                 "norm": "cv", "n": 9, "types": [0], "type": 0},
            ],
            "widget": [
                {"tag": "#widget-beta", "slug": "widget-beta", "child": "beta",
                 "norm": "beta", "n": 5, "types": [1], "type": 1},
            ],
        },
        "orphans": [{"tag": "#stray", "slug": "stray", "n": 2, "types": [0],
                     "type": 0}],
        "pairs": {"alpha": {"slug": "阿尔法", "lang": "zh"},
                  "阿尔法": {"slug": "alpha", "lang": "en"}},
        "local_new": [], "generated_ts": 0, "count": 7, "source": "api",
    }


class TestStripTagEmoji:
    """_strip_tag_emoji() — trailing emoji removal."""

    @pytest.mark.parametrize("tag,expected", [
        ("demo📦", "demo"),
        ("unread", "unread"),
        ("demo-cv🧇", "demo-cv"),
        ("x🧑‍💻", "x"),          # ZWJ composite (Cf)
        ("demo-1⃣", "demo-1"),    # keycap (Me)
        ("y❤️", "y"),        # VS16 (Mn)
        ("", ""),
    ])
    def test_strip(self, zot, tag, expected):
        assert zot._strip_tag_emoji(tag) == expected

    def test_only_strips_trailing(self, zot):
        """Interior emoji/连字符 must survive — only the tail is stripped."""
        assert zot._strip_tag_emoji("a📦b") == "a📦b"


class TestTagNamingLaw:
    """`/` = level-1 root, `#` = `#<root>-<child>`, case-insensitive."""

    def test_root_slug(self, zot):
        assert zot._root_slug("/demo📦") == "demo"
        assert zot._root_slug("/DEMO📦") == "demo"
        assert zot._root_slug("/unread") == "unread"

    def test_root_slug_rejects_non_root(self, zot):
        assert zot._root_slug("#demo-alpha") == ""
        assert zot._root_slug("demo") == ""
        assert zot._root_slug(None) == ""

    def test_child_body(self, zot):
        assert zot._child_body("#demo-alpha🧇") == "demo-alpha"
        assert zot._child_body("#demo-Alpha") == "demo-alpha"
        assert zot._child_body("/demo📦") == ""

    def test_child_sigil_helpers(self, zot):
        assert zot._child_root_slug("#demo-alpha") == "demo"
        assert zot._child_slug("#demo-alpha-beta") == "alpha-beta"
        assert zot._child_slug("#demo-alpha") == "alpha"

    def test_tag_key_unifies_emoji_variants(self, zot):
        """#demo-alpha and #demo-alpha🧇 are the same tag for dedup purposes."""
        assert zot._tag_key("#demo-alpha") == zot._tag_key("#demo-alpha🧇")
        assert zot._tag_key("#demo-Alpha") == zot._tag_key("#demo-alpha")


class TestParseVocab:
    """_parse_vocab() — /tags response → roots / children / orphans."""

    def test_same_name_type_0_and_1_are_summed(self, zot):
        """Zotero stores type 0 and 1 as separate rows for the same name."""
        vocab = zot._parse_vocab([
            {"tag": "/demo📦", "meta": {"numItems": 10, "type": 0}},
            {"tag": "/demo📦", "meta": {"numItems": 7, "type": 1}},
        ])
        root = vocab["roots"][0]
        assert root["n"] == 17
        assert root["types"] == [0, 1]

    def test_spaced_tags_dropped(self, zot):
        vocab = zot._parse_vocab([
            {"tag": "some phrase tag", "meta": {"numItems": 99, "type": 1}},
            {"tag": "/demo📦", "meta": {"numItems": 1, "type": 0}},
        ])
        assert vocab["count"] == 1

    def test_children_grouped_under_longest_root(self, zot):
        """Root slug attribution uses longest-prefix, not first-hyphen."""
        vocab = zot._parse_vocab([
            {"tag": "/a📦", "meta": {"numItems": 5, "type": 0}},
            {"tag": "/a-b📦", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#a-b-child", "meta": {"numItems": 3, "type": 0}},
        ])
        assert "a-b" in vocab["children"]
        assert vocab["children"]["a-b"][0]["child"] == "child"

    def test_unresolvable_child_becomes_orphan(self, zot):
        vocab = zot._parse_vocab([
            {"tag": "/demo📦", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#stray", "meta": {"numItems": 3, "type": 0}},
        ])
        assert [o["tag"] for o in vocab["orphans"]] == ["#stray"]
        assert vocab["children"].get("demo", []) == []

    def test_duplicate_page_does_not_double_count(self, zot):
        """Ties under sort=numItems can repeat a row across pages."""
        vocab = zot._parse_vocab([
            {"tag": "/demo📦", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#demo-alpha", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#demo-alpha", "meta": {"numItems": 5, "type": 0}},
        ])
        assert vocab["children"]["demo"][0]["n"] == 5

    def test_norm_groups_near_duplicates(self, zot):
        vocab = zot._parse_vocab([
            {"tag": "/demo📦", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#demo-alpha", "meta": {"numItems": 5, "type": 0}},
            {"tag": "#demo-alphas", "meta": {"numItems": 2, "type": 0}},
        ])
        norms = {c["norm"] for c in vocab["children"]["demo"]}
        assert norms == {"alpha"}

    def test_status_root_flagged(self, zot):
        vocab = zot._parse_vocab([
            {"tag": "/unread", "meta": {"numItems": 9, "type": 0}},
        ])
        assert vocab["roots"][0]["status"] is True


class TestTagMatcher:
    """infer_tags_structured() / _merge_tag_plan() — reuse-first matching."""

    def test_reuses_existing_root_and_child(self, zot):
        plan = zot.infer_tags_structured("A deep dive into demo alpha internals",
                                         vocab=_synthetic_vocab())
        assert plan["root"] == "/demo📦"
        assert "#demo-alpha" in plan["children"]
        assert "#demo-alpha" in plan["attached"]

    def test_chinese_alias_bridges_to_english_slug(self, zot):
        plan = zot.infer_tags_structured("阿尔法 算法详解",
                                         vocab=_synthetic_vocab())
        assert plan["root"] == "/demo📦"

    def test_abbreviation_alias(self, zot):
        """`cv` must match "computer vision" via _CHILD_SLUG_ALIASES."""
        plan = zot.infer_tags_structured("Modern computer vision pipelines",
                                         vocab=_synthetic_vocab())
        assert "#demo-cv" in plan["children"]

    def test_registered_pair_adds_both_languages(self, zot):
        """A registered pair whose counterpart exists in the library → both."""
        plan = zot.infer_tags_structured("demo alpha notes",
                                         vocab=_synthetic_vocab())
        assert "#demo-alpha" in plan["children"]
        assert "#demo-阿尔法" in plan["children"]

    def test_unregistered_pair_is_not_invented(self, zot):
        """#demo-cv has no registered pair → no fabricated Chinese sibling."""
        plan = zot.infer_tags_structured("demo cv internals",
                                         vocab=_synthetic_vocab())
        assert "#demo-cv" in plan["children"]
        assert not any("中文" in c for c in plan["children"])
        assert len([c for c in plan["children"] if c.startswith("#demo-")]) <= 3

    def test_status_root_never_selected(self, zot):
        """`/unread` is a workflow tag, not a topic root."""
        plan = zot.infer_tags_structured("unread", vocab=_synthetic_vocab())
        assert plan["root"] is None
        assert plan["children"] == []
        assert plan["mode"] == "none"

    def test_status_slug_never_minted_as_new_root(self, zot):
        """The fallback path must not mint /unread🔗 either."""
        vocab = _synthetic_vocab()
        vocab["roots"] = [r for r in vocab["roots"] if r["slug"] != "unread"]
        plan = zot.infer_tags_structured("unread", vocab=vocab)
        assert plan["root"] is None

    def test_existing_slug_is_reused_not_reminted(self, zot):
        """An emoji difference must not create a second root with the same slug."""
        vocab = _synthetic_vocab()
        plan = zot.infer_tags_structured("Demo material for beginners",
                                         vocab=vocab)
        assert plan["root"] == "/demo📦"
        assert plan["mode"] == "existing"

    def test_no_match_creates_new_root_with_topic_slug(self, zot):
        """Nothing in the library matches → new root using the canonical slug."""
        plan = zot.infer_tags_structured(
            "Recent advances in machine learning", vocab=_synthetic_vocab())
        assert plan["root"] == "/ai🤖"
        assert plan["mode"] == "new-topic"

    def test_url_only_returns_empty(self, zot):
        plan = zot.infer_tags_structured("https://example.com/some/post",
                                         vocab=_synthetic_vocab())
        assert plan["root"] is None
        assert plan["children"] == []
        assert plan["mode"] == "url_only"

    def test_vocab_unavailable_invents_nothing(self, zot):
        """No vocabulary → no tags at all. Inventing is what caused divergence."""
        plan = zot.infer_tags_structured("anything at all",
                                         vocab=zot._empty_vocab("vocab_unavailable"))
        assert plan["root"] is None
        assert plan["children"] == []
        assert plan["mode"] == "vocab_unavailable"

    def test_manual_tag_beats_equal_strength_automatic(self, zot):
        """auto-only tags carry imported metadata junk → down-weighted.

        Equal text evidence (both slugs are whole words) and equal counts;
        only ``type`` differs, so ordering isolates the W_AUTO_ONLY factor.
        """
        vocab = _synthetic_vocab()
        vocab["children"]["demo"].append(
            {"tag": "#demo-beta", "slug": "demo-beta", "child": "beta",
             "norm": "beta", "n": 30, "types": [1], "type": 1})
        plan = zot.infer_tags_structured("demo alpha beta", vocab=vocab)
        assert plan["children"][0] == "#demo-alpha"
        assert "#demo-beta" in plan["children"]

    def test_children_capped_at_seven(self, zot):
        vocab = _synthetic_vocab()
        vocab["children"]["demo"] = [
            {"tag": f"#demo-c{i}", "slug": f"demo-c{i}", "child": f"c{i}",
             "norm": f"c{i}", "n": 20, "types": [0], "type": 0}
            for i in range(12)]
        plan = zot._merge_tag_plan(
            zot.infer_tags_structured("demo c0 c1 c2 c3 c4 c5 c6 c7 c8 c9 c10",
                                      vocab=vocab))
        assert len(plan["children"]) == 7

    def test_root_and_children_are_disjoint(self, zot):
        plan = zot.infer_tags_structured("demo alpha", vocab=_synthetic_vocab())
        assert plan["root"] not in plan["children"]

    def test_needs_reports_unpaired_concept(self, zot):
        plan = zot.infer_tags_structured("demo cv internals",
                                         vocab=_synthetic_vocab())
        assert any(n["concept"] == "cv" and n["want_lang"] == "zh"
                   for n in plan["needs"])

    def test_new_is_disjoint_from_attached(self, zot):
        plan = zot.infer_tags_structured("demo alpha", vocab=_synthetic_vocab())
        assert not (set(plan["new"]) & set(plan["attached"]))


class TestMergeTagPlan:
    """_merge_tag_plan() — single point guaranteeing output shape."""

    def _plan(self, **kw):
        base = {"root": "/demo📦", "root_slug": "demo", "children": [],
                "needs": [], "attached": [], "mode": "existing", "scores": {}}
        base.update(kw)
        return base

    def test_dedupes_by_tag_key(self, zot):
        plan = zot._merge_tag_plan(
            self._plan(children=["#demo-alpha", "#demo-alpha🧇"]))
        assert plan["children"] == ["#demo-alpha"]

    def test_child_equal_to_root_dropped(self, zot):
        plan = zot._merge_tag_plan(self._plan(children=["/demo📦"]))
        assert plan["children"] == []

    def test_user_hash_hint_takes_precedence(self, zot):
        plan = zot._merge_tag_plan(self._plan(children=["#demo-alpha"]),
                                   ["#custom-x"])
        assert plan["children"][0] == "#custom-x"
        assert "#demo-alpha" in plan["children"]

    def test_bare_word_hint_qualified_with_root_slug(self, zot):
        """A bare hint must not become a rootless orphan."""
        plan = zot._merge_tag_plan(self._plan(), ["bareword"])
        assert plan["children"] == ["#demo-bareword"]

    def test_cap_applies_after_hints(self, zot):
        plan = zot._merge_tag_plan(
            self._plan(children=[f"#demo-c{i}" for i in range(10)]),
            ["#custom-x"])
        assert len(plan["children"]) == 7
        assert "#custom-x" in plan["children"]


class TestInferTagsBackCompat:
    """infer_tags() — the flat wrapper for v2.5.0-and-earlier callers."""

    def test_returns_flat_list(self, zot, monkeypatch):
        monkeypatch.setattr(zot, "load_vocab", lambda *a, **k: _synthetic_vocab())
        tags = zot.infer_tags("A deep dive into demo alpha internals")
        assert tags[0] == "/demo📦"
        assert "#demo-alpha" in tags
        assert all(isinstance(t, str) for t in tags)

    def test_flat_list_has_no_duplicates(self, zot, monkeypatch):
        monkeypatch.setattr(zot, "load_vocab", lambda *a, **k: _synthetic_vocab())
        tags = zot.infer_tags("demo alpha")
        assert len(tags) == len(set(tags))


class TestVocabCacheRoundTrip:
    """Disk cache under ZOTERO_VOCAB_DIR — never inside the repo."""

    def test_write_then_read(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        vocab = _synthetic_vocab()
        assert zot._write_vocab_file(vocab)
        assert zot._vocab_path().startswith(str(tmp_path))
        assert zot._read_vocab_file()["roots"][0]["tag"] == "/demo📦"

    def test_corrupt_file_degrades_to_none(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        with open(zot._vocab_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        assert zot._read_vocab_file() is None

    def test_note_new_tags_makes_them_reusable(self, zot, tmp_path, monkeypatch):
        """The whole point: a tag invented now must be reusable next run."""
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        zot._write_vocab_file(_synthetic_vocab())
        zot._vocab_note_new_tags(["/fresh🎈", "#fresh-thing"])
        disk = zot._read_vocab_file()
        assert any(r["tag"] == "/fresh🎈" for r in disk["roots"])
        assert "/fresh🎈" in disk["local_new"]
        # and the matcher now sees it
        plan = zot.infer_tags_structured("a post about fresh thing", vocab=disk)
        assert plan["root"] == "/fresh🎈"
        assert "#fresh-thing" in plan["children"]

    def test_pairs_file_round_trip(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        with open(zot._pairs_path(), "w", encoding="utf-8") as f:
            f.write('{"pairs": [{"en": "demo-alpha", "zh": "demo-阿尔法"}]}')
        pairs = zot._load_pairs()
        assert pairs["demo-alpha"]["slug"] == "demo-阿尔法"
        assert pairs["demo-阿尔法"]["lang"] == "en"

    def test_missing_pairs_degrades_to_empty(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        assert zot._load_pairs() == {}


class TestResolveAliasesTagVerbs:
    """New tag verbs must not be rewritten to `tag search`."""

    @pytest.mark.parametrize("verb", ["vocab", "suggest", "candidates", "merge"])
    def test_new_verb_survives(self, zot, verb):
        argv = zot._resolve_aliases(["zot", "tag", verb, "x"])
        assert argv[:3] == ["zot", "tag", verb]

    def test_unknown_verb_still_becomes_search(self, zot):
        """Backward compat: `zot tag /unread` is still a tag search."""
        argv = zot._resolve_aliases(["zot", "tag", "/unread"])
        assert argv[:3] == ["zot", "tag", "search"]


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


class TestPreprintFromUrl:
    """_preprint_from_url() — 学术预印本 URL → repository 判定。

    回归（v2.4.0）：arxiv.org/pdf/* 等二进制论文链接被归档成 webpage。
    itemType 必须按 URL 判定，抓取失败也不能回退成 webpage。
    """

    @pytest.mark.parametrize("url,expected", [
        ("https://arxiv.org/abs/2608.20711", "arXiv"),
        ("https://arxiv.org/pdf/2608.20711", "arXiv"),
        ("https://www.arxiv.org/abs/2301.00001", "arXiv"),
        ("https://export.arxiv.org/pdf/2301.00001", "arXiv"),
        ("https://www.biorxiv.org/content/10.1101/2023.01.01.522000v1", "bioRxiv"),
        ("https://www.medrxiv.org/content/10.1101/2023.01.01.522000v1", "medRxiv"),
        ("https://chemrxiv.org/engage/chemrxiv/article-details/abc", "ChemRxiv"),
        ("https://www.ssrn.com/abstract=1234567", "SSRN"),
    ])
    def test_preprint_domains(self, zot, url, expected):
        assert zot._preprint_from_url(url) == expected

    @pytest.mark.parametrize("url", [
        # substring 假阳性场景（必须 None，修复前会误判）
        "https://arxiv.org.evil.com/phish",   # endswith .evil.com，不是 .arxiv.org
        "https://fakearxiv.org/abs/1234",     # 包含 "arxiv.org" 但不是 arxiv.org 子域
        "https://notbiorxiv.org/page",        # substring 包含 biorxiv.org
        "https://example.com/paper.pdf",      # 普通网页/文档
        "",
        None,
    ])
    def test_non_preprint(self, zot, url):
        assert zot._preprint_from_url(url) is None, f"Should not match: {url}"


class TestArxivAbsUrl:
    """_arxiv_abs_url() — arxiv /pdf/ 下载链接 → /abs/ 摘要页（元数据抓取源）。

    PDF 是二进制，没有 <title>/<meta>；归档前把元数据源改写为摘要页。
    附件下载仍走原 /pdf/ URL（见 _arxiv_pdf_url），两者方向相反。
    """

    def test_pdf_to_abs(self, zot):
        assert zot._arxiv_abs_url(
            "https://arxiv.org/pdf/2608.20711") == "https://arxiv.org/abs/2608.20711"

    def test_pdf_with_query_string(self, zot):
        assert zot._arxiv_abs_url(
            "https://arxiv.org/pdf/2608.20711?download=true") == "https://arxiv.org/abs/2608.20711"

    def test_abs_unchanged(self, zot):
        url = "https://arxiv.org/abs/2608.20711"
        assert zot._arxiv_abs_url(url) == url

    def test_non_arxiv_unchanged(self, zot):
        url = "https://example.com/paper.pdf"
        assert zot._arxiv_abs_url(url) == url


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
