# zot-tool Test Suite

Integration + unit tests for `scripts/zot.py`. Tests run against your own Zotero test library.

- **Test framework**: pytest

## Setting Up Your Test Library

1. **Create a Zotero group** at https://www.zotero.org/groups/new (or use a user library)
   - Name it something obvious like `zot-tool-test`
   - Get the library ID from the URL: `https://www.zotero.org/groups/<ID>/...`

2. **Generate an API key** at https://www.zotero.org/settings/keys
   - Check "Allow group access" (if using a group)
   - Select your test group under "Group Permissions"
   - Grant Read/Write access

3. **Configure environment**:
   ```bash
   # Required
   export ZOTERO_API_KEY="your-key-here"              # Linux/macOS
   export ZOTERO_TEST_LIBRARY_ID="1234567"             # your test library ID
   # Optional — defaults to "group"
   export ZOTERO_TEST_LIBRARY_TYPE="group"             # "group" or "user"
   ```

4. **Run the tests**:
   ```bash
   cd path/to/zot-tool
   python -m pytest tests/ -v
   ```

The test suite auto-creates prerequisite collections (`Test-Forbidden`, `Test-Misc`) and cleans up all test-created items. No manual setup needed beyond the API key.

## Quick Start

```bash
# Set your env vars
export ZOTERO_API_KEY="your-key-here"
export ZOTERO_TEST_LIBRARY_ID="your-test-library-id"

# Run
python -m pytest tests/ -v

# Unit tests only (no API/network needed):
python -m pytest tests/test_unit.py -v

# Clean up leftover test data:
python tests/cleanup.py
```

## Test Structure

```
tests/
├── conftest.py           # Shared fixtures (env setup, collection management, cleanup)
├── test_unit.py          # Pure logic — no API access (fast, always runnable)
├── test_smoke.py         # Basic connectivity + forbidden exclusion + cache
├── test_items.py         # Item CRUD: add, delete, duplicate detection
├── test_tags.py          # Tag management: add, remove, set, list (v1.10.0)
├── test_note.py          # Note creation (setnote) + attachment listing + detach
├── test_search.py        # Search & browse: search, coll, list, tag search
├── cleanup.py            # Utility: delete leftover items/collections
└── README.md             # This file
```

## Test Coverage

### ✅ Tested

| Feature | Tests | Notes |
|---------|-------|-------|
| `add` (create item) | `test_items.py` | Item creation, unread tag, collection assignment, extra_json |
| `search` / `list` / `coll` / `tag` (browse) | `test_search.py` | Full-text search, collection browse, tag search, sort order |
| `tags` / `tag add/remove/set` | `test_tags.py` | Full CRUD, idempotency, clear |
| `note set` (setnote) | `test_note.py` | Direct note content (no LLM) |
| `attachment list` (attachments) | `test_note.py` | List all children (notes + attachments) |
| `attachment remove` (detach) | `test_note.py` | Delete child items |
| `item remove` (delete) | `test_items.py` | Item deletion |
| `archive_url` (metadata + dup check) | `test_items.py` | Duplicate URL detection, title-based archive |
| Forbidden exclusion | `test_smoke.py` | Items in forbidden collection hidden from list/search |
| `_all_collections()` cache | `test_smoke.py` | TTL cache, force_refresh, invalidation |
| Pure logic functions | `test_unit.py` | Domain mapping, URL detection, emoji, concept extraction, etc. |

### ❌ Not Tested (requires external tools)

| Feature | Missing dependency | How to test manually |
|---------|-------------------|---------------------|
| `archive` (offline copy) | monolith + WebDAV | Archive a real URL with monolith installed |
| `attachment add` / `attachment update` | WebDAV | Configure `ZOTERO_WEBDAV_URL/USER/PASS` |
| `note add` (LLM summarization) | minis-model-use | Run `zot note add <key>` with LLM configured |

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `ZOTERO_API_KEY` | **Yes** | — | API key with read/write access to test library |
| `ZOTERO_TEST_LIBRARY_ID` | **Yes** | — | Your test library ID |
| `ZOTERO_TEST_LIBRARY_TYPE` | No | `group` | `group` or `user` |
| `ZOTERO_TEST_FORBIDDEN_NAME` | No | `Test-Forbidden` | Name of the placeholder forbidden collection |
| `ZOTERO_TEST_MISC_NAME` | No | `Test-Misc` | Name of the Misc parent collection |

All other env vars (`ZOTERO_LIBRARY_ID`, `ZOTERO_FORBIDDEN_COLLECTION`, etc.) are set automatically by `conftest.py`.

## Adding New Tests

1. Import fixtures from `conftest.py`:
   - `zot_mod` — the imported zot module (call its functions directly)
   - `api_client` — raw pyzotero client for verification
   - `misc_key` / `forbidden_key` — prerequisite collection keys
   - `tracked_items` — append created item keys for auto-cleanup

2. For API tests: use `tracked_items.append(key)` to ensure cleanup

3. For pure logic tests: import `zot_mod` only, no API access needed

4. For tests that need external tools (monolith, WebDAV, LLM): skip with `pytest.skip()` and document the prerequisite in the skip message
