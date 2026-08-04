"""Note tests — setnote (direct), attachments listing, detach.

What's NOT tested here (requires external tools):
  - addnote — uses minis-model-use for LLM summarization
  - attach / reattach — requires WebDAV (ZOTERO_WEBDAV_URL/USER/PASS)
"""

import pytest


@pytest.fixture
def parent_item(zot_mod, api_client, misc_key, tracked_items):
    """Create a parent item for note attachment tests."""
    resp = api_client.create_items([{
        "itemType": "webpage",
        "title": "Test Item — Note Operations",
        "url": "https://test.invalid/note-ops",
        "collections": [misc_key],
        "tags": [{"tag": "/unread", "type": 1}],
    }])
    assert resp.get("successful"), f"Failed to create parent item: {resp}"
    key = resp["successful"]["0"]["key"]
    tracked_items.append(key)
    return key


class TestSetNote:
    """zot setnote <key> [content] — directly set note content (no LLM)."""

    def test_setnote_creates_note_child(self, zot_mod, api_client, parent_item):
        """setnote creates a note child item with the given HTML content."""
        note_html = "<p>Test note content — <strong>bold</strong> and <em>italic</em>.</p>"
        zot_mod.setnote(parent_item, note_html)

        # Verify via API: find note children
        children = api_client.children(parent_item)
        notes = [c for c in children
                 if c.get("data", {}).get("itemType") == "note"]
        assert len(notes) >= 1, f"No note child found. Children: {children}"

        note_data = notes[0].get("data", {})
        note_content = note_data.get("note", "")
        assert "bold" in note_content, (
            f"Note content mismatch: {note_content[:100]}"
        )

    def test_setnote_multiple_notes(self, zot_mod, api_client, parent_item):
        """Multiple setnote calls create multiple note children."""
        zot_mod.setnote(parent_item, "<p>First note</p>")
        zot_mod.setnote(parent_item, "<p>Second note</p>")

        children = api_client.children(parent_item)
        notes = [c for c in children
                 if c.get("data", {}).get("itemType") == "note"]
        assert len(notes) >= 2, (
            f"Expected >=2 notes, got {len(notes)}"
        )

    def test_setnote_no_content_error(self, zot_mod, capsys):
        """setnote with no content prints an error (no crash)."""
        zot_mod.setnote("NONEXISTENT99", "")
        captured = capsys.readouterr()
        assert "Error" in captured.out or "no note" in captured.out.lower()


class TestAttachmentsListing:
    """zot attachments <key> — list child items (attachments + notes)."""

    def test_list_attachments(self, zot_mod, api_client, parent_item, capsys):
        """attachments() lists all child items of a parent."""
        # First add a note so there's something to list
        zot_mod.setnote(parent_item, "<p>Test note for listing</p>")

        zot_mod.list_attachments(parent_item)
        captured = capsys.readouterr()
        # Should show the note child
        assert "note" in captured.out.lower() or "📝" in captured.out

    def test_list_attachments_empty(self, zot_mod, api_client, misc_key,
                                     tracked_items, capsys):
        """attachments() on an item with no children shows 'No children'."""
        resp = api_client.create_items([{
            "itemType": "webpage",
            "title": "Test Item — No Children",
            "url": "https://test.invalid/no-children",
            "collections": [misc_key],
        }])
        assert resp.get("successful")
        key = resp["successful"]["0"]["key"]
        tracked_items.append(key)

        zot_mod.list_attachments(key)
        captured = capsys.readouterr()
        assert "No children" in captured.out

    def test_list_attachments_bad_key(self, zot_mod, capsys):
        """attachments() on a nonexistent key prints error (no crash)."""
        zot_mod.list_attachments("NONEXISTENT99")
        captured = capsys.readouterr()
        assert "Failed" in captured.out or "❌" in captured.out


class TestDetach:
    """zot detach <child-key> — delete child items."""

    def test_detach_note(self, zot_mod, api_client, parent_item):
        """Detaching a note child removes it."""
        # Create a note
        zot_mod.setnote(parent_item, "<p>Note to detach</p>")
        children = api_client.children(parent_item)
        notes = [c for c in children
                 if c.get("data", {}).get("itemType") == "note"]
        assert len(notes) >= 1, "Note was not created"
        note_key = notes[0]["key"]

        # Detach it
        zot_mod.detach_attachment(note_key)

        # Verify gone
        children_after = api_client.children(parent_item)
        note_keys_after = [c["key"] for c in children_after
                           if c.get("data", {}).get("itemType") == "note"]
        assert note_key not in note_keys_after, (
            f"Note {note_key} was not detached. Remaining notes: {note_keys_after}"
        )

    def test_detach_no_parent_warns(self, zot_mod, capsys):
        """Detaching a top-level item prints warning (no crash)."""
        zot_mod.detach_attachment("NONEXISTENT99")
        captured = capsys.readouterr()
        # Either "no parent" warning or "Failed" error — both OK
        assert any(w in captured.out.lower() for w in
                   ["no parent", "failed", "use 'zot delete'"])
