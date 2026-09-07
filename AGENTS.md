# zot-tool

Single-file Python CLI (`scripts/zot.py`, ~3,100 lines) for Zotero library
management via `pyzotero` + Zotero Web API v3. Also contains an OpenCLI skill
definition (`SKILL.md`). This file is the single source of guidance for AI
coding agents and contributors working in this repository.

## Commands

Canonical form is `zot <noun> <verb> [args]`:

```bash
# Canonical form
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

No build step, no linter configured. Test suite in `tests/` (pytest, unit +
integration). CI via `.github/workflows/test.yml`. Git hooks live in `scripts/`
(`pre-commit`, `commit-msg`) — see [Git hooks](#git-hooks).

## Environment variables

The script exits immediately if any of these are missing:

| Variable | Purpose |
|---|---|
| `ZOTERO_API_KEY` | Zotero API key |
| `ZOTERO_LIBRARY_ID` | user/group library ID |
| `ZOTERO_FORBIDDEN_COLLECTION` | `🙊Personal` collection key; all sub-collections and items recursively excluded from search/list/output |
| `ZOTERO_MISC_COLLECTION` | fallback collection key for unmatched archive items |

Optional:

| Variable | Purpose |
|---|---|
| `ZOTERO_WEBDAV_URL` / `ZOTERO_WEBDAV_USER` / `ZOTERO_WEBDAV_PASS` | WebDAV endpoint + credentials for offline ZIP uploads |
| `ZOTERO_ARCHIVE_TRIGGER` | defaults to `【归档到Zotero】` |
| `ZOTERO_OFFLINE_DIR` | local fallback dir for offline HTML when WebDAV is unavailable (default `/tmp/zotero-offline`) |

## Dependencies

- **pyzotero** ≥ 1.11.0 — pip install if missing (Zotero API v3; older versions lack the `timeout=` param)
- **monolith** — system binary for HTML archive capture; `scripts/zot.py` checks availability via `which`/`where`
- **minis-model-use** — CLI for LLM summarization (optional; graceful fallback when unavailable)
- **curl** — metadata fetching, WebDAV uploads, HN Algolia API calls

## Architecture

### CLI dispatch (`__main__` block)

Three-step flow: `_resolve_aliases()` rewrites legacy command names →
`_build_parser()` builds the argparse tree → dispatch by `args.command` and
`args.action`.

Canonical `<noun> <verb>` structure:

| Noun | Verbs |
|------|-------|
| `item` | `add`, `remove`, `list`, `search`, `archive` |
| `tag` | `add`, `remove`, `set`, `list`, `search` |
| `coll` | `list`, `remove`, `search` |
| `note` | `add` (LLM), `set` (raw) |
| `attachment` | `add`, `remove`, `update`, `list` |

Legacy aliases (e.g. `search` → `item search`, `tags` → `tag list`,
`attach` → `attachment add`) are handled by `_resolve_aliases()` before
argparse sees them.

### Archive workflow (the core feature)

`archive_url()` orchestrates a multi-step pipeline:

1. **Metadata fetch** (`fetch_url_metadata`): curl + regex title/description
   extraction. Apple Podcasts via iTunes API, Hacker News via Algolia API
   (`_fetch_hn_thread_info`). Cloudflare-block detection.
2. **Tag inference** (`infer_tags`): fuzzy-matches against existing library
   tags first (`_fuzzy_match_existing`), falls back to concept extraction with
   emoji suffix (`_extract_concepts`). User-supplied `#tag` hints take
   priority, merged to max 3.
3. **Collection matching** (three-tier, highest priority first):
   - Domain hard-mapping: `_find_existing_domain_collection()` checks the
     `DOMAIN_TO_SUBCOLL` dict (33 platform → short-name mappings) against
     existing `Misc--<sub>` collections.
   - Multi-signal scoring: `find_best_collection()` — keyword intersection
     between text and collection name/content.
   - Fallback: `create_misc_subcollection()` creates a new `Misc--xxx` under
     the MISC_COLLECTION parent.
4. **Duplicate check**: searches by URL before creating.
5. **Item creation**: pyzotero `create_items` with `/unread` tag + inferred/user tags.
6. **Offline copy** (`save_offline_copy`): auto-detects binary (PDF/EPUB) vs
   HTML. Binary → direct download + WebDAV upload. HTML → `monolith` capture →
   optional WeChat post-processing → ZIP + WebDAV upload with `.prop` sidecar.
   Falls back to local disk when WebDAV is unavailable.
7. **Content note** (`_create_content_note`): LLM summarization via
   `minis-model-use` CLI, with rule-based fallback
   (`_build_minimal_fallback_note`).

### Collection infrastructure

- `_all_collections()` — paginated fetch via `zot.everything()` with a 5-minute
  TTL cache. Replaces bare `zot.collections()`, which defaults to limit=100 and
  silently truncates large libraries.
- `_invalidate_collections_cache()` — called after write operations to force refresh.
- `coll remove <key>` — deletes a single empty collection via the raw Zotero
  API (needs the version header for `If-Unmodified-Since-Version`). Replaces
  the removed `cleanup-empty-collections` command.

### Key caches and globals (module-level)

- `_forbidden_item_keys` — lazy-loaded set of item keys in `🙊Personal` and all descendant collections.
- `_forbidden_collection_keys` — lazy-loaded set of collection keys (root + all descendants); used by `list_collections()`, `find_best_collection()`, `get_forbidden_items()`.
- `_invalidate_forbidden_cache()` — resets both forbidden caches after membership changes.
- `_existing_tags_cache` — lazy-loaded list of all library tags.
- `_collections_cache` — TTL-cached result of `_all_collections()`.
- `_last_fetched_description` — set by `archive_url()`, read by `_create_content_note()`.
- `_cached_hn_info` — set by `archive_url()` for HN posts, read by `_create_content_note()`.

### WeChat MP post-processing (`_fix_wechat_html`, v1.8.2)

Monolith-saved WeChat articles have JS-dependent hidden content. This function
strips `visibility:hidden`/`opacity:0` from `#js_content`, removes `data-src`
from `<img>` tags (keeping the inline base64 `src` — those are real image data,
not placeholders), and strips WeChat debugging attributes.

### Domain hard-mapping (`DOMAIN_TO_SUBCOLL`)

Platform → short-name mapping used by both `_find_existing_domain_collection()`
(matching) and `_domain_subcoll_name()` (naming). Adding a new platform requires
updating the dict and the SKILL.md domain-mapping docs.

## Conventions

- All new items are auto-tagged `/unread`; remove the tag after processing.
- Tags: no spaces, `#` prefix, emoji suffix, max 3 per item. Prefer matching
  existing library tags; fallback format is `#<domain>🤖` or
  `#<domain>-<subdomain>🤖`.
- `🙊Personal` collection and all descendants are universally excluded.
- Misc sub-collections use the `Misc--<shortname>` naming convention.
- Offline archives use `monolith` for HTML; `scripts/upload-skill.sh` syncs the
  OpenCLI skill.

## Versioning

- The version number is recorded in the `SKILL.md` frontmatter `version:` field
  (single source of truth). Current version: **v2.3.5**.
- Format: `MAJOR.MINOR.PATCH`

| Part | Meaning | Example |
|------|---------|---------|
| MAJOR | Major refactor / breaking change | `v2.0.0` argparse migration |
| MINOR | New feature / subcommand | `1.9.0` → `1.10.0` |
| PATCH | Bug fix within a version | `1.8.1` → `1.8.2` |

Rules:

- Bump the version whenever a feature PR is merged.
- If a feature branch has multiple commits, tag the version only at the final merge.
- PATCH fixes may be noted at the end of a commit message (e.g. `(v1.9.1)`) or omitted.
- Full changelog lives in SKILL.md under `## 版本历史`.

Recent history (highlights; see SKILL.md for details):

- **v2.3.5** — `alanzucconi.com → alanzucconi` domain mapping
- **v2.3.4** — `barrd.dev → barrd` domain mapping
- **v2.3.3** — `gatesnotes.com → gatesnotes` domain mapping (Cloudflare-blocked)
- **v2.3.2** — `infinitelymore.xyz → infinitelymore` domain mapping
- **v2.3.1** — integration test assertion fixes; auto-release creates GitHub Release

## Commit message format

Follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

[body — only when extra context is needed]
```

| type | Use for |
|------|---------|
| `feat` | New feature / new command |
| `fix` | Bug fix |
| `refactor` | Refactor (behavior unchanged) |
| `docs` | Pure documentation change |
| `chore` | Everything else (dependency updates, scripts, etc.) |

**Scope** distinguishes the code layer being changed:

| scope | Corresponds to |
|-------|----------------|
| `zot` | `scripts/zot.py` logic changes |
| `skill` | `SKILL.md` / skill definition changes |
| `zot,skill` | Both at once (e.g. a feature shipped with doc updates) |
| `hook` | Git hook scripts (pre-commit, commit-msg) |

**Body template** (required — the hook enforces the 3Cs sections, subject ≤ 60 chars):

```
Changes
-------
* <action> <what> [detail]
  Use precise verbs: Add, Remove, Fix, Rewrite, Extract, Bump

Context
-------
* Problem: <what issue or requirement prompted this change>
* Previous state: <what existed before>
* New approach: <why this approach is better>
* Rationale: <non-obvious design decisions or trade-offs>

Considerations
---------------
* Backward compatibility: <what existing behavior is preserved>
* Version impact: <MAJOR/MINOR/PATCH bump>
* Edge cases: <boundary conditions to verify>
* Cleanup: <dead code or stale docs to remove>
```

## Git hooks

Install once after a fresh clone:

```bash
# Unix / Git Bash
cp scripts/pre-commit .git/hooks/pre-commit
cp scripts/commit-msg  .git/hooks/commit-msg
chmod +x .git/hooks/pre-commit .git/hooks/commit-msg
```

```powershell
# PowerShell
Copy-Item scripts\pre-commit .git\hooks\pre-commit
Copy-Item scripts\commit-msg  .git\hooks\commit-msg
```

### pre-commit

Scans the staged diff and blocks commits that contain sensitive material:

- `ZOTERO_API_KEY` assignment with a value
- Nutstore / WebDAV personal paths
- Hardcoded collection key values (8-char uppercase alphanumeric strings in a
  value position, not the env-var name)
- `ZOTERO_WEBDAV_URL/USER/PASS` credential assignments
- `ZOTERO_LIBRARY_ID` numeric assignment

On a hit it prints the offending lines and rejects the commit. Emergency
bypass: `git commit --no-verify`.

### commit-msg

Validates the message against [Commit message format](#commit-message-format):

| Check | Rule |
|-------|------|
| Format | `type(scope): subject` |
| type | Must be one of `feat`/`fix`/`refactor`/`docs`/`chore`/`test`/`style`/`perf`/`ci`/`build`/`revert` |
| scope | Must be in `zot`/`skill`/`hook` (comma-separated for multi-layer, e.g. `zot,skill`) |
| Length | subject ≤ 60 chars |
| Trailing punctuation | subject must not end with `.!?;` |
| Blank line | blank line required between subject and body |
| Body width | each body line ≤ 72 chars |
| 3Cs sections | body must contain `Changes`, `Context`, and `Considerations` headings |
| `@` residue | blocks a subject starting with `@` or a body ending with `@` (Bash here-string misuse) |

Emergency bypass: `git commit --no-verify`.
