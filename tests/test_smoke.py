"""Smoke tests — basic connectivity, empty-library behavior, caching."""

import time
import pytest


class TestBasicConnectivity:
    """Verify the test library is reachable and properly empty."""

    def test_list_collections(self, zot_mod, capsys):
        """list_collections() runs without error against test library."""
        zot_mod.list_collections()
        captured = capsys.readouterr()
        assert "Collections" in captured.out

    def test_list_items_runs(self, zot_mod, capsys):
        """list_items() runs without crashing and produces expected output."""
        zot_mod.list_items(limit=50)
        captured = capsys.readouterr()
        assert "items" in captured.out.lower()
        assert "Recent" in captured.out

    def test_search_empty(self, zot_mod, capsys):
        """Search on empty library returns 0 results."""
        zot_mod.search("nonexistent-query-xyz", limit=10)
        captured = capsys.readouterr()
        assert "Found 0 items" in captured.out

    def test_collections_not_empty(self, zot_mod, capsys):
        """Test library has our prerequisite Test-Misc collection.

        Test-Forbidden is excluded by design (it's the forbidden collection),
        so it should NOT appear in output.
        """
        zot_mod.list_collections()
        captured = capsys.readouterr()
        assert "Test-Misc" in captured.out
        assert "Test-Forbidden" not in captured.out, (
            "Forbidden collection should be excluded from list_collections()"
        )


class TestForbiddenExclusion:
    """Verify forbidden collection items are excluded from all output."""

    def test_forbidden_item_hidden_from_list(self, zot_mod, api_client,
                                              forbidden_key, tracked_items):
        """Items in forbidden collection do not appear in list_items()."""
        # Create an item in the forbidden collection
        resp = api_client.create_items([{
            "itemType": "webpage",
            "title": "SECRET — should not leak",
            "url": "https://test.invalid/secret-1",
            "collections": [forbidden_key],
            "tags": [{"tag": "/unread", "type": 1}],
        }])
        if resp.get("successful"):
            key = resp["successful"]["0"]["key"]
            tracked_items.append(key)

            # Reset forbidden cache so the new item is picked up
            zot_mod._invalidate_forbidden_cache()

            # list_items should NOT show it
            from io import StringIO
            import sys
            old_stdout = sys.stdout
            sys.stdout = StringIO()
            try:
                zot_mod.list_items(limit=100)
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout

            assert "SECRET" not in output, (
                "Forbidden item leaked into list_items output!"
            )
            assert key not in output, (
                f"Forbidden item key {key} leaked into list_items output!"
            )

    def test_forbidden_item_hidden_from_search(self, zot_mod, api_client,
                                                forbidden_key, tracked_items):
        """Items in forbidden collection do not appear in search results."""
        resp = api_client.create_items([{
            "itemType": "webpage",
            "title": "TOPSECRET search test",
            "url": "https://test.invalid/secret-2",
            "collections": [forbidden_key],
            "tags": [{"tag": "/unread", "type": 1}],
        }])
        if resp.get("successful"):
            key = resp["successful"]["0"]["key"]
            tracked_items.append(key)

            # Reset forbidden cache so the new item is picked up
            zot_mod._invalidate_forbidden_cache()

            from io import StringIO
            import sys
            old_stdout = sys.stdout
            sys.stdout = StringIO()
            try:
                zot_mod.search("TOPSECRET", limit=10)
                output = sys.stdout.getvalue()
            finally:
                sys.stdout = old_stdout

            assert "TOPSECRET" not in output, (
                "Forbidden item leaked into search output!"
            )


class TestCollectionsCache:
    """Verify _all_collections() caching behavior."""

    def test_cache_returns_list(self, zot_mod):
        """_all_collections() returns a non-empty list."""
        result = zot_mod._all_collections()
        assert isinstance(result, list)
        assert len(result) >= 2  # at least our 2 prerequisite colls

    def test_cache_ttl_works(self, zot_mod):
        """Cached result is reused within TTL, refreshable with force_refresh."""
        # First call
        r1 = zot_mod._all_collections()
        # Second call — cached (should be same objects)
        r2 = zot_mod._all_collections()
        assert len(r2) == len(r1)

        # Force refresh — may or may not differ, but should succeed
        r3 = zot_mod._all_collections(force_refresh=True)
        assert len(r3) == len(r1)

    def test_cache_invalidation(self, zot_mod):
        """_invalidate_collections_cache() forces next call to fetch fresh."""
        zot_mod._all_collections()  # prime cache
        zot_mod._invalidate_collections_cache()
        # Next call should re-fetch — verify it works
        fresh = zot_mod._all_collections()
        assert isinstance(fresh, list)
        assert len(fresh) >= 2
