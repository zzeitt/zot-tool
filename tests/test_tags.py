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


def _json_from(text):
    """Parse the JSON blob out of a command's stdout (warnings may precede it)."""
    import json
    start = text.find("{")
    assert start >= 0, f"no JSON in output: {text!r}"
    return json.loads(text[start:])


class TestTagVocab:
    """zot tag vocab — derived high-frequency vocabulary (v2.5.0)."""

    def test_cache_path_outside_repo(self, zot_mod, capsys):
        """The vocab cache must live outside the repo — it holds library data."""
        zot_mod.tag_vocab(cache_path=True)
        path = capsys.readouterr().out.strip()
        assert "zot_vocab" in path
        assert "zot-tool" not in path.replace("\\", "/"), \
            f"cache path is inside the repo: {path}"

    def test_vocab_fetch(self, zot_mod, capsys):
        """A fetched vocabulary satisfies the structural invariants."""
        zot_mod.tag_vocab(refresh=True, as_json=True)
        vocab = _json_from(capsys.readouterr().out)
        for key in ("roots", "children", "orphans", "pairs", "local_new"):
            assert key in vocab, f"missing {key}"
        assert vocab["count"] >= len(vocab["roots"])
        root_slugs = {r["slug"] for r in vocab["roots"]}
        for root_slug, children in vocab["children"].items():
            assert root_slug in root_slugs, \
                f"children under unknown root {root_slug!r}"
            for c in children:
                assert c["tag"].startswith("#")
                assert c["tag"].count(" ") == 0
        for r in vocab["roots"]:
            assert r["tag"].startswith("/")
            assert r["status"] is (r["slug"] in zot_mod._STATUS_TAG_SLUGS)

    def test_vocab_cache_reuse(self, zot_mod):
        """A second load inside the TTL is served from disk, not the API."""
        zot_mod.load_vocab(force_refresh=True)
        # Drop the in-memory copy so the disk path is exercised.
        zot_mod._vocab_cache["data"] = None
        vocab = zot_mod.load_vocab()
        assert vocab["source"] == "disk"
        assert vocab["roots"], "disk cache came back empty"


class TestTagSuggest:
    """zot tag suggest — dry run, never writes to the library."""

    def test_suggest_shape(self, zot_mod, capsys):
        zot_mod.tag_suggest("A synthetic title about zot test widgets",
                            as_json=True)
        plan = _json_from(capsys.readouterr().out)
        final = plan["final"]
        assert len(plan["children"]) <= 7
        assert len(final) == len(set(final)), f"duplicates in {final}"
        if plan["root"]:
            assert plan["root"].startswith("/")
            assert plan["root"] not in plan["children"]
        assert plan["children"] == [t for t in plan["children"] if t.startswith("#")]

    def test_suggest_url_only_yields_nothing(self, zot_mod):
        final = zot_mod.tag_suggest("https://test.invalid/some/post")
        assert final == []


class TestTagMerge:
    """zot tag merge <old> <new> — consolidate divergent legacy tags."""

    OLD = "#test-merge-old🤖"
    NEW = "#test-merge-new🤖"

    def test_merge_dry_run(self, zot_mod, api_client, test_item, capsys):
        zot_mod.tags_add(test_item, self.OLD)
        zot_mod.tag_merge(self.OLD, self.NEW, dry_run=True)
        assert "DRY RUN" in capsys.readouterr().out
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert self.OLD in tags, "dry run must not write"

    def test_merge_real(self, zot_mod, api_client, test_item):
        zot_mod.tags_add(test_item, self.OLD)
        updated = zot_mod.tag_merge(self.OLD, self.NEW)
        assert updated >= 1
        items = api_client.item(test_item)
        item = items[0] if isinstance(items, list) else items
        tags = [t.get("tag") for t in item.get("data", {}).get("tags", [])]
        assert self.NEW in tags
        assert self.OLD not in tags, f"old tag still present: {tags}"
        assert tags.count(self.NEW) == 1

    def test_merge_noop(self, zot_mod, capsys):
        """Merging a tag nobody carries is a no-op, not an error."""
        assert zot_mod.tag_merge("#test-merge-absent🤖", self.NEW) == 0
        assert "Nothing to update" in capsys.readouterr().out
