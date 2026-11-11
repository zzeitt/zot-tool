"""Regression tests — collections disk cache must not cause duplicate collections.

2026-11-11 事故：三个并行会话各建了一个 ``Misc--infinitelymore`` 集合。
根因：``_invalidate_collections_cache()`` 写磁盘快照时把 ``ts`` 覆盖成
``time.time()``（而不是它想写的 0），于是快照「标着新鲜、内容过期」——
TTL 之内每次 CLI 启动都命中这份缺目标名字的快照，
``create_misc_subcollection`` 于是判定「不存在」→ 建出同名重复集合。
"""

import json
import os
import time

import pytest


class TestCollectionsCacheInvalidation:
    """_invalidate_collections_cache() 必须真正把磁盘快照标记为过期。"""

    def test_invalidate_marks_disk_snapshot_stale(self, zot, tmp_path):
        """Invalidate 后磁盘 ts 必须是 0（过期），不能是 time.time()。

        回归点：旧实现无条件写 ts=time.time()，快照因此被误判为新鲜，
        内容却是旧的 → 并行会话建出同名重复 collection。
        """
        cache_dir = tmp_path / "zot-cache"
        cache_dir.mkdir()
        cache_file = cache_dir / "collections.json"
        monkey = pytest.MonkeyPatch()
        monkey.setenv("ZOTERO_CACHE_DIR", str(cache_dir))
        try:
            # 写一份「旧」快照
            zot._write_collections_cache(88015, [{"key": "OLD1"}])
            disk = zot._read_collections_cache()
            assert disk is not None
            assert disk["version"] == 88015
            assert disk["collections"] == [{"key": "OLD1"}]
            assert disk["ts"] > 0, "正常写入的快照应当带当前时间戳"

            # 失效
            zot._invalidate_collections_cache()

            disk2 = zot._read_collections_cache()
            assert disk2 is not None
            assert disk2["ts"] == 0, (
                "失效后磁盘快照必须被标记为过期(ts=0)；"
                f"实际 ts={disk2['ts']} —— 会被 TTL 当成新鲜快照，"
                "导致看不到别处刚建的 collection"
            )
            # version 必须保留：下次走 since= 增量而不是全量
            assert disk2["version"] == 88015
            assert disk2["collections"] == [{"key": "OLD1"}]
        finally:
            monkey.undo()

    def test_write_accepts_explicit_ts(self, zot, tmp_path):
        """_write_collections_cache(ts=0) 必须原样写入 0。"""
        cache_dir = tmp_path / "zot-cache"
        cache_dir.mkdir()
        monkey = pytest.MonkeyPatch()
        monkey.setenv("ZOTERO_CACHE_DIR", str(cache_dir))
        try:
            zot._write_collections_cache(123, [{"key": "X"}], ts=0)
            disk = zot._read_collections_cache()
            assert disk["ts"] == 0
            assert disk["version"] == 123
        finally:
            monkey.undo()

    def test_write_defaults_to_now(self, zot, tmp_path):
        """不传 ts 时默认写当前时间。"""
        cache_dir = tmp_path / "zot-cache"
        cache_dir.mkdir()
        monkey = pytest.MonkeyPatch()
        monkey.setenv("ZOTERO_CACHE_DIR", str(cache_dir))
        try:
            before = time.time()
            zot._write_collections_cache(123, [{"key": "X"}])
            disk = zot._read_collections_cache()
            assert disk["ts"] >= before
        finally:
            monkey.undo()


class TestDuplicateCollectionGuard:
    """按名字查 collection 时，查不到必须强制刷新一次再判。"""

    def test_find_collection_by_name_refreshes_when_missing(self, zot):
        """快照里没有目标名字时，_find_collection_by_name 必须强制刷新。

        用 monkeypatch 模拟「磁盘快照过期（缺目标）」+「服务端其实有」。
        """
        stale = [{"key": "AAA", "data": {"name": "Misc--other"}}]

        calls = {"n": 0}

        def fake_all(force_refresh=False):
            calls["n"] += 1
            if not force_refresh:
                return stale
            return [{"key": "BBB", "data": {"name": "Misc--infinitelymore"}}]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(zot, "_all_collections", fake_all)
        try:
            key = zot._find_collection_by_name("Misc--infinitelymore")
            assert key == "BBB", "强制刷新后应命中真实存在的集合"
            assert calls["n"] >= 2, "第一次查不到必须触发一次强制刷新"
        finally:
            monkey.undo()

    def test_find_collection_by_name_no_refresh_when_present(self, zot):
        """快照里有目标名字时直接返回，不多刷一次。"""
        calls = {"n": 0}

        def fake_all(force_refresh=False):
            calls["n"] += 1
            return [{"key": "AAA", "data": {"name": "Misc--infinitelymore"}}]

        monkey = pytest.MonkeyPatch()
        monkey.setattr(zot, "_all_collections", fake_all)
        try:
            assert zot._find_collection_by_name("Misc--infinitelymore") == "AAA"
            assert calls["n"] == 1
        finally:
            monkey.undo()

    def test_find_collection_by_name_returns_none_when_absent(self, zot):
        """真的不存在时返回 None（允许创建）。"""
        monkey = pytest.MonkeyPatch()
        monkey.setattr(
            zot, "_all_collections",
            lambda force_refresh=False: [{"key": "A", "data": {"name": "X"}}])
        try:
            assert zot._find_collection_by_name("Misc--nope") is None
        finally:
            monkey.undo()

    def test_create_misc_reuses_existing_not_duplicate(self, zot):
        """create_misc_subcollection 命中已有集合时不得再调 create_collections。"""
        created = {"called": False}

        def fake_all(force_refresh=False):
            return [{"key": "EXIST", "data": {"name": "Misc--infinitelymore"}}]

        def fake_create(collections):
            created["called"] = True
            return {"successful": {"0": {"key": "NEW"}}}

        monkey = pytest.MonkeyPatch()
        monkey.setattr(zot, "_all_collections", fake_all)
        monkey.setattr(zot.zot, "create_collections", fake_create)
        try:
            key = zot.create_misc_subcollection(
                "https://www.infinitelymore.xyz/p/some-post")
            assert key == "EXIST"
            assert not created["called"], "已有同名集合时不得再建一个"
        finally:
            monkey.undo()
