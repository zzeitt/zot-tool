"""性能/可靠性改动的回归测试（2026-10-31，由 systematic-debugging 驱动）。

覆盖：
  - URL 规范化 + 去重键（与 zot_guard 同一套规则）
  - collections 增量合并（含 deleted）
  - guard 索引增量合并（含移入黑名单）
  - guard 缓存读取不受 TTL 影响（否则增量路径永远进不去）
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

import zot_guard as _guard_mod


@pytest.fixture()
def zot_guard():
    return _guard_mod


# ---------------------------------------------------------------------------
# URL 规范化 / 去重键
# ---------------------------------------------------------------------------

class TestNormalizeUrlKey:
    def test_strips_tracking_params(self, zot):
        key = zot._normalize_url_key(
            "https://www.quantamagazine.org/how-voting-is-like-a-klein-bottle-20261026/"
            "?utm_source=Quanta&utm_campaign=x&mc_cid=fd51014c4d&mc_eid=abc")
        assert key == "quantamagazine.org/how-voting-is-like-a-klein-bottle-20261026"

    def test_keeps_semantic_query_and_sorts(self, zot):
        assert (zot._normalize_url_key("https://example.com/p?b=2&a=1")
                == zot._normalize_url_key("https://example.com/p?a=1&b=2"))
        assert "id=50237224" in zot._normalize_url_key(
            "https://news.ycombinator.com/item?id=50237224")

    def test_www_scheme_fragment_equivalent(self, zot):
        a = zot._normalize_url_key("https://www.example.com/a/b/")
        b = zot._normalize_url_key("http://example.com/a/b#frag")
        assert a == b == "example.com/a/b"

    def test_different_path_is_different_article(self, zot):
        assert (zot._normalize_url_key("https://a.com/x")
                != zot._normalize_url_key("https://a.com/y"))


# ---------------------------------------------------------------------------
# collections 增量合并
# ---------------------------------------------------------------------------

class TestMergeCollections:
    def _c(self, key, name="n", deleted=False):
        d = {"name": name}
        if deleted:
            d["deleted"] = True
        return {"key": key, "data": d}

    def test_added_and_updated(self, zot):
        cached = [self._c("A", "old"), self._c("B")]
        merged = zot._merge_by_key(cached, [self._c("A", "new"), self._c("C")])
        by_key = {c["key"]: c["data"]["name"] for c in merged}
        assert by_key == {"A": "new", "B": "n", "C": "n"}

    def test_deleted_is_removed(self, zot):
        cached = [self._c("A"), self._c("B")]
        merged = zot._merge_by_key(cached, [self._c("B", deleted=True)])
        assert [c["key"] for c in merged] == ["A"]


# ---------------------------------------------------------------------------
# guard 缓存与增量
# ---------------------------------------------------------------------------

class TestGuardCache:
    def test_stale_blob_is_still_returned(self, zot_guard, tmp_path, monkeypatch):
        """_load_cache 不管新鲜度 —— 否则拿不到 version，增量永远进不去。"""
        p = tmp_path / "cache.json"
        p.write_text('{"ts": 0, "version": 42, "items": [{"key": "X"}]}', encoding="utf-8")
        # _cache_path() 优先读环境变量（conftest 会设一个）、环境变量没有才用
        # 模块常量 —— 这里直接指到环境变量，避免和 conftest 的临时目录打架。
        monkeypatch.setenv("ZOTERO_GUARD_CACHE", str(p))
        blob = zot_guard._load_cache()
        assert blob is not None
        assert blob["version"] == 42

    def test_corrupt_blob_returns_none(self, zot_guard, tmp_path, monkeypatch):
        p = tmp_path / "cache.json"
        p.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("ZOTERO_GUARD_CACHE", str(p))
        assert zot_guard._load_cache() is None


class TestGuardMergeItems:
    def _item(self, key, url="https://a.com/x", deleted=False, cols=None, parent=None):
        d = {"key": key, "url": url, "title": "t", "collections": cols or []}
        if deleted:
            d["deleted"] = True
        if parent:
            d["parentItem"] = parent
        return {"key": key, "data": d}

    def test_deleted_dropped(self, zot_guard):
        cached = [{"key": "A", "url": "https://a.com/x", "title": "t"},
                  {"key": "B", "url": "https://b.com/y", "title": "t"}]
        merged = zot_guard._merge_items(cached, [self._item("B", deleted=True)], set())
        assert [i["key"] for i in merged] == ["A"]

    def test_new_item_added(self, zot_guard):
        merged = zot_guard._merge_items([], [self._item("C")], set())
        assert [i["key"] for i in merged] == ["C"]

    def test_item_moved_into_forbidden_is_dropped(self, zot_guard):
        cached = [{"key": "A", "url": "https://a.com/x", "title": "t"}]
        merged = zot_guard._merge_items(
            cached, [self._item("A", cols=["BANNED"])], {"BANNED"})
        assert merged == []

    def test_child_item_ignored(self, zot_guard):
        merged = zot_guard._merge_items(
            [], [self._item("D", parent="PARENT")], set())
        assert merged == []
