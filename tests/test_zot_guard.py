"""zot_guard.py 的单元测试 —— 纯逻辑，不需要 Zotero API。

覆盖：
  - URL 规范化（去跟踪参数 / 去 www. / 去尾斜杠 / query 排序）
  - 重复判定键（host+path，跨 host 不算重复）
  - 标题分词（中英混合）
  - 疑似同题检测的稀有词规则（避免 "rare" 这类噪声词）
"""

import os
import sys

import pytest

scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import zot_guard  # noqa: E402


class TestNormalizeUrl:
    def test_strips_utm_and_mc_params(self):
        url = ("https://www.quantamagazine.org/how-voting-is-like-a-klein-bottle-20261026/"
               "?utm_source=Quanta+Magazine&utm_campaign=fd51014c4d-Quanta_Weekly"
               "&utm_medium=email&utm_term=0_f0cb&mc_cid=fd51014c4d&mc_eid=xyz")
        host, rest, canon = zot_guard.normalize_url(url)
        assert host == "quantamagazine.org"
        assert "utm_" not in rest and "mc_cid" not in rest and "mc_eid" not in rest
        assert canon == "//quantamagazine.org/how-voting-is-like-a-klein-bottle-20261026"

    def test_keeps_semantic_query(self):
        """?id= / ?v= 是 URL 语义的一部分，必须保留。"""
        _, _, canon = zot_guard.normalize_url("https://news.ycombinator.com/item?id=50237224")
        assert canon == "//news.ycombinator.com/item?id=50237224"
        _, _, canon = zot_guard.normalize_url("https://www.youtube.com/watch?v=abc123")
        assert "v=abc123" in canon

    def test_drops_www_scheme_fragment_trailing_slash(self):
        a = zot_guard.url_key("https://www.example.com/a/b/")
        b = zot_guard.url_key("http://example.com/a/b#section")
        assert a == b == "example.com/a/b"

    def test_query_param_order_irrelevant(self):
        a = zot_guard.url_key("https://example.com/p?b=2&a=1")
        b = zot_guard.url_key("https://example.com/p?a=1&b=2")
        assert a == b


class TestUrlKey:
    def test_same_path_different_host_is_not_duplicate(self):
        assert zot_guard.url_key("https://a.com/x") != zot_guard.url_key("https://b.com/x")

    def test_different_path_same_host_is_not_duplicate(self):
        assert zot_guard.url_key("https://a.com/x") != zot_guard.url_key("https://a.com/y")

    def test_empty_input(self):
        assert zot_guard.url_key("") is None


class TestTokenizeTitle:
    def test_english_words(self):
        toks = zot_guard.tokenize_title("The Klein Bottle")
        assert "klein" in toks and "bottle" in toks

    def test_chinese_bigrams(self):
        toks = zot_guard.tokenize_title("克莱因瓶")
        assert "克莱" in toks and "因瓶" in toks


class TestTitleCandidates:
    """疑似同题检测：共享低频判别词才算命中。"""

    def _items(self):
        return [
            {"key": "AAAA", "title": "How Voting Is Like a Klein Bottle", "url": "https://q.com/a", "date": "2026-01-01"},
            {"key": "BBBB", "title": "The Best Rare Guide", "url": "https://q.com/b", "date": "2026-01-01"},
        ]

    def _df(self):
        # "klein"/"bottle"/"voting" 只出现一次 → 低频判别词；"rare" 也只一次但长度 < 5
        df = {}
        for t in ("klein", "bottle", "voting", "how", "is", "like", "a",
                  "the", "best", "rare", "guide"):
            df[t] = 1
        df["the"] = 50
        df["a"] = 50
        return df

    def test_shared_rare_words_flagged(self):
        out = zot_guard.check_title_candidates(
            "https://q.com/new",
            {}, self._df(), self._items(),
            "How Voting Is Like a Klein Bottle",
        )
        assert any(o["key"] == "AAAA" for o in out)

    def test_short_common_word_alone_not_flagged(self):
        """只有 "rare"（长度 < 5）这种噪声词，不应判为疑似。"""
        out = zot_guard.check_title_candidates(
            "https://q.com/new", {}, self._df(), self._items(),
            "A Rare Thing",
        )
        assert not any(o["key"] == "BBBB" for o in out)

    def test_same_url_excluded(self):
        out = zot_guard.check_title_candidates(
            "https://q.com/a", {}, self._df(), self._items(),
            "How Voting Is Like a Klein Bottle",
        )
        assert not any(o["key"] == "AAAA" for o in out)


class TestSlugToTitle:
    def test_strips_date_suffix(self):
        assert zot_guard.slug_to_title(
            "https://q.com/how-voting-is-like-a-klein-bottle-20261026/") == \
            "how voting is like a klein bottle"

    def test_empty(self):
        assert zot_guard.slug_to_title("https://q.com/") == ""
