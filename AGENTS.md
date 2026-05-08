# zot-tool

Single-file Python CLI (`scripts/zot.py`) for Zotero library management via pyzotero + Zotero API v3.

## Run

```bash
python3 scripts/zot.py <command> [args]
alias zot="python3 scripts/zot.py"
```

## Required env vars (checked at startup — script exits if missing)

- `ZOTERO_API_KEY`
- `ZOTERO_LIBRARY_ID`
- `ZOTERO_FORBIDDEN_COLLECTION` — 🙊Personal collection key; all its sub-collections/items are excluded
- `ZOTERO_MISC_COLLECTION` — fallback collection for unmatched archive items

## Optional env vars

- `ZOTERO_ARCHIVE_TRIGGER` — default `【归档到Zotero】`
- `ZOTERO_WEBDAV_URL` / `ZOTERO_WEBDAV_USER` / `ZOTERO_WEBDAV_PASS` — WebDAV for offline ZIP upload
- `ZOTERO_OFFLINE_DIR` — local fallback dir for offline HTML (default `/tmp/zotero-offline`)

## Key conventions

- All new items auto-tagged `/unread`; remove when processed
- Forbidden collection (🙊Personal) and all its sub-collections are always excluded from search/list
- Tag matching: prefer existing tags (no spaces), fallback format `#领域-子领域🤖` or `#领域🤖` (max 3)
- Offline archive uses `monolith` to save HTML; `scripts/upload-skill.sh` updates the OpenCLI skill

## Dependencies

- `pyzotero` — install via pip if not present
- `monolith` — for HTML archive saving (called via subprocess)