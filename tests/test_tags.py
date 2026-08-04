"""Tag management tests — tags, tag add/remove/set (v1.10.0)."""

import pytest


@pytest.fixture
def test_item(zot_mod, api_client, misc_key, tracked_items):
    """Create a test item for tag operations. Returns (item_key, api_client)."""
    resp = api_client.create_items([{
        "itemType": "webpage",
        "title": "Test Item — Tag Ops",
        "url": "https://test.invalid/tag-ops",
        "collections": [misc_key],
        "tags": [{"tag": "/unread", "type": 1}],
    }])
    assert resp.get("successful"), f"Failed to create test item: {resp}"
    key = resp["successful"]["0"]["key"]
    tracked_items.append(key)
    return key


class TestTagsList:
    """zot tags <key> — list tags on an item."""

    def test_list_tags(self, zot_mod, test_item, capsys):
        """tags_list() shows tags for an item."""
        zot_mod.tags_list(test_item)
        captured = capsys.readouterr()
        assert "/unread" in captured.out

    def test_list_tags_no_tags_item(self, zot_mod, api_client, misc_key,
                                     tracked_items, capsys):
        """tags_list() on an item with no tags shows 'No tags'."""
        resp = api_client.create_items([{
            "itemType": "webpage",
            "title": "Test Item — No Tags",
            "url": "https://test.invalid/no-tags",
            "collections": [misc_key],
        }])
        assert resp.get("successful")
        key = resp["successful"]["0"]["key"]
        tracked_items.append(key)

        zot_mod.tags_list(key)
        captured = capsys.readouterr()
        assert "No tags" in captured.out


class TestTagAdd:
    """zot tag add <key> <tag>... — add tags to item."""

    def test_add_tag(self, zot_mod, api_client, test_item):
        """Adding a tag persists on the item."""
        zot_mod.tags_add(test_item, "#test-add🤖")
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert "#test-add🤖" in tags, f"Tag not added. Tags: {tags}"

    def test_add_duplicate_idempotent(self, zot_mod, api_client, test_item):
        """Adding the same tag twice doesn't create duplicates."""
        tag = "#test-dup🤖"
        zot_mod.tags_add(test_item, tag)
        zot_mod.tags_add(test_item, tag)  # second add

        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        count = tags.count(tag)
        assert count == 1, f"Tag '{tag}' appears {count} times (expected 1)"

    def test_add_multiple_tags(self, zot_mod, api_client, test_item):
        """Adding multiple tags at once works."""
        zot_mod.tags_add(test_item, "#tag-a🤖", "#tag-b💻")
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert "#tag-a🤖" in tags
        assert "#tag-b💻" in tags


class TestTagRemove:
    """zot tag remove <key> <tag>... — remove tags from item."""

    def test_remove_tag(self, zot_mod, api_client, test_item):
        """Removing a tag removes it from the item."""
        # First add a tag to remove
        zot_mod.tags_add(test_item, "#test-rm🤖")

        zot_mod.tags_remove(test_item, "#test-rm🤖")
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert "#test-rm🤖" not in tags, f"Tag not removed. Tags: {tags}"

    def test_remove_nonexistent_tag(self, zot_mod, test_item, capsys):
        """Removing a tag that doesn't exist prints a warning."""
        zot_mod.tags_remove(test_item, "#does-not-exist🤖")
        captured = capsys.readouterr()
        assert "nothing to remove" in captured.out.lower() or \
               "not" in captured.out.lower()


class TestTagSet:
    """zot tag set <key> <tag>... — replace all tags."""

    def test_set_tags_replace(self, zot_mod, api_client, test_item):
        """Setting tags replaces all existing tags."""
        zot_mod.tags_set(test_item, "#only-this🤖")
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert tags == ["#only-this🤖"], f"Tags not replaced. Got: {tags}"

    def test_set_tags_clear(self, zot_mod, api_client, test_item):
        """Setting with no args clears all tags."""
        zot_mod.tags_set(test_item)  # no args = clear
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert tags == [], f"Tags not cleared. Got: {tags}"

    def test_set_tags_with_multiple(self, zot_mod, api_client, test_item):
        """Setting multiple tags replaces all with the new set."""
        zot_mod.tags_set(test_item, "#tag-1🤖", "#tag-2💻", "#tag-3💰")
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert set(tags) == {"#tag-1🤖", "#tag-2💻", "#tag-3💰"}
