# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Single-file Python CLI (`scripts/zot.py`, ~2680 lines) for Zotero library management via `pyzotero` + Zotero Web API v3. Uses argparse for CLI dispatch with `<noun> <verb>` canonical form. Also contains an OpenCLI skill definition (`SKILL.md`).

## Commands

```bash
# Canonical form: zot <noun> <verb> [args]
python3 scripts/zot.py <noun> <verb> [args]

# Common alias
alias zot="python3 scripts/zot.py"

# Examples
zot item search "machine learning"
zot item archive "https://example.com"
zot tag add KC5ETPXM "#AI🤖"
zot coll list
zot tag "/unread"
```

No build step, no linter configured. Test suite in `tests/` (pytest, unit + integration). CI via `.github/workflows/test.yml`.

## Required environment variables

The script exits immediately if any of these are missing:
- `ZOTERO_API_KEY` — Zotero API key
- `ZOTERO_LIBRARY_ID` — user/group library ID
- `ZOTERO_FORBIDDEN_COLLECTION` — `🙊Personal` collection key; all sub-collections and items recursively excluded from search/list/output
- `ZOTERO_MISC_COLLECTION` — fallback collection key for unmatched archive items

Optional (WebDAV attachment uploads):
- `ZOTERO_WEBDAV_URL`, `ZOTERO_WEBDAV_USER`, `ZOTERO_WEBDAV_PASS`

Optional (behavior):
- `ZOTERO_ARCHIVE_TRIGGER` — defaults to `【归档到Zotero】`
- `ZOTERO_OFFLINE_DIR` — local fallback for offline HTML when WebDAV unavailable

## Dependencies

- **pyzotero** ≥ 1.11.0 (pip install if missing; Zotero API v3)
- **monolith** — system binary for HTML archive capture (`scripts/zot.py` checks availability via `which`/`where`)
- **minis-model-use** — CLI for LLM summarization (optional; graceful fallback when unavailable)
- **curl** — for metadata fetching, WebDAV uploads, HN Algolia API calls
- `package.json` / `node_modules` — only contains `@anthropic-ai/claude-agent-sdk` (used by the Claude Code / OpenCLI integration, not by the Python script itself)

## Architecture

### CLI dispatch (`__main__` block, ~line 2578)

Three-step flow: `_resolve_aliases()` rewrites legacy command names → `_build_parser()` builds argparse tree → dispatch by `args.command` and `args.action`.

Canonical `<noun> <verb>` structure:

| Noun | Verbs |
|------|-------|
| `item` | `add`, `remove`, `list`, `search`, `archive` |
| `tag` | `add`, `remove`, `set`, `list`, `search` |
| `coll` | `list`, `remove`, `search` |
| `note` | `add` (LLM), `set` (raw) |
| `attachment` | `add`, `remove`, `update`, `list` |

17 legacy aliases (eg `search` → `item search`, `tags` → `tag list`, `attach` → `attachment add`) handled by `_resolve_aliases()` before argparse sees them.

### Archive workflow (the core feature)

`archive_url()` orchestrates a multi-step pipeline:
1. **Metadata fetch** (`fetch_url_metadata`): curl + regex title/description extraction. Apple Podcasts via iTunes API, Hacker News via Algolia API (`_fetch_hn_thread_info`). Cloudflare-block detection.
2. **Tag inference** (`infer_tags`): fuzzy-matches against existing library tags first (`_fuzzy_match_existing`), falls back to concept extraction with emoji suffix (`_extract_concepts`). User-supplied `#tag` hints take priority, merged to max 3.
3. **Collection matching** (three-tier, highest priority first):
   - Domain hard-mapping: `_find_existing_domain_collection()` checks `DOMAIN_TO_SUBCOLL` dict (17 platforms → short names) against existing `Misc--<sub>` collections
   - Multi-signal scoring: `find_best_collection()` — keyword intersection between text and collection name/content
   - Fallback: `create_misc_subcollection()` creates new `Misc--xxx` under the MISC_COLLECTION parent
4. **Duplicate check**: searches by URL before creating
5. **Item creation**: pyzotero `create_items` with `/unread` tag + inferred/user tags
6. **Offline copy** (`save_offline_copy`): auto-detects binary (PDF/EPUB) vs HTML. Binary → direct download + WebDAV upload. HTML → `monolith` capture → optional WeChat post-processing → ZIP + WebDAV upload with `.prop` sidecar. Falls back to local disk when WebDAV unavailable.
7. **Content note** (`_create_content_note`): LLM summarization via `minis-model-use` CLI, with rule-based fallback (`_build_minimal_fallback_note`).

### Collection infrastructure

- `_all_collections()` — paginated fetch via `zot.everything()` with 5-minute TTL cache. Replaces bare `zot.collections()` (which defaults to limit=100 and silently truncates large libraries).
- `_invalidate_collections_cache()` — called after write operations to force refresh.
- `coll remove <key>` — deletes a single empty collection via raw Zotero API (needs version header for `If-Unmodified-Since-Version`). Replaces the removed `cleanup-empty-collections` command.

### Key caches and globals (module-level)

- `_forbidden_item_keys` — lazy-loaded set of item keys in `🙊Personal` and all descendant collections
- `_forbidden_collection_keys` — lazy-loaded set of collection keys (root + all descendants); used by `list_collections()`, `find_best_collection()`, `get_forbidden_items()`
- `_invalidate_forbidden_cache()` — resets both forbidden caches after membership changes
- `_existing_tags_cache` — lazy-loaded list of all library tags
- `_collections_cache` — TTL-cached result of `_all_collections()`
- `_last_fetched_description` — set by `archive_url()`, read by `_create_content_note()`
- `_cached_hn_info` — set by `archive_url()` for HN posts, read by `_create_content_note()`

### WeChat MP post-processing (`_fix_wechat_html`, v1.8.2)

Monolith-saved WeChat articles have JS-dependent hidden content. This function strips `visibility:hidden`/`opacity:0` from `#js_content`, removes `data-src` from `<img>` tags (keeping the inline base64 `src` — those are real image data, not placeholders), and strips WeChat debugging attributes.

### Domain hard-mapping (`DOMAIN_TO_SUBCOLL`)

17-platform mapping used by both `_find_existing_domain_collection()` (match) and `_domain_subcoll_name()` (naming). Adding a new platform requires updating the dict and SKILL.md.

## Conventions

- All new items auto-tagged `/unread`; user removes tag after processing
- Tags: no spaces, `#` prefix, emoji suffix, max 3 per item. Prefer matching existing library tags.
- `🙊Personal` collection and all descendants are universally excluded
- Misc sub-collections use `Misc--<shortname>` naming convention

## Version history (recent, from SKILL.md)

- **v2.0.0** — current; argparse migration, `<noun> <verb>` unification, forbidden subcollection fix, test suite + CI
- **v1.8.5** — previous stable
- **v1.8.3** — LLM summarization for items with empty description
- **v1.8.2** — Fixed v1.8.1 data-loss bug (was replacing base64 inlined images with external URLs)
- **v1.8.1** — WeChat MP HTML post-processing
- **v1.8.0** — Domain hard-mapping, `_all_collections()` pagination, `cleanup-empty-collections` command
