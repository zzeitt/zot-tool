#!/usr/bin/env python3
"""Zotero CLI with collection/tag search and 🙊Personal exclusion"""
import argparse
import os
import sys
import re
import json
import subprocess
import html
import tempfile
from urllib.parse import urlparse
from pyzotero import zotero
from pyzotero._utils import build_url
try:
    import httpx
except ImportError:
    # pyzotero ≥1.15 把 HTTP 依赖从 httpx 更名为 httpx2；两者 API 兼容，
    # 统一以 httpx 名使用（zot.client.timeout 赋值等）。
    import httpx2 as httpx

IS_WINDOWS = sys.platform == "win32"

def _get_temp_dir():
    """Get platform-appropriate temp directory"""
    if IS_WINDOWS:
        return tempfile.gettempdir()
    return "/tmp"

def _get_offline_dir():
    """Get offline storage directory"""
    if IS_WINDOWS:
        default_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp", "zotero-offline")
        return os.environ.get("ZOTERO_OFFLINE_DIR", default_dir)
    return os.environ.get("ZOTERO_OFFLINE_DIR", "/tmp/zotero-offline")

def _get_vocab_dir():
    """Get the library-derived data directory.

    Deliberately OUTSIDE the repository. This directory holds data derived
    from the user's own Zotero library (tag names + frequencies, domain
    overrides) that must never be committed to the public repo.
    """
    default_dir = os.path.join(_get_temp_dir(), "zot_vocab")
    return os.environ.get("ZOTERO_VOCAB_DIR", default_dir)

def _get_domain_map_path():
    """Path to the local domain-override overlay (untracked, outside the repo)."""
    return os.environ.get("ZOTERO_DOMAIN_MAP",
                          os.path.join(_get_vocab_dir(), "domain_overrides.json"))

LIBRARY_ID = os.environ.get("ZOTERO_LIBRARY_ID")
API_KEY = os.environ.get("ZOTERO_API_KEY")
FORBIDDEN_COLLECTION = os.environ.get("ZOTERO_FORBIDDEN_COLLECTION")
MISC_COLLECTION = os.environ.get("ZOTERO_MISC_COLLECTION")
ARCHIVE_TRIGGER = os.environ.get("ZOTERO_ARCHIVE_TRIGGER", "【归档到Zotero】")

if not API_KEY:
    print("Error: ZOTERO_API_KEY not set")
    sys.exit(1)

if not LIBRARY_ID:
    print("Error: ZOTERO_LIBRARY_ID not set")
    sys.exit(1)

if not FORBIDDEN_COLLECTION:
    print("Error: ZOTERO_FORBIDDEN_COLLECTION not set")
    sys.exit(1)

if not MISC_COLLECTION:
    print("Error: ZOTERO_MISC_COLLECTION not set")
    sys.exit(1)

LIBRARY_TYPE = os.environ.get("ZOTERO_LIBRARY_TYPE", "user")
if LIBRARY_TYPE not in ("user", "group"):
    print(f"Error: ZOTERO_LIBRARY_TYPE must be 'user' or 'group', got '{LIBRARY_TYPE}'")
    sys.exit(1)
zot = zotero.Zotero(LIBRARY_ID, LIBRARY_TYPE, API_KEY)
# httpx 默认超时仅 5s，写大 note（内嵌 base64 图，可达 MB 级）会抛
# WriteTimeout: The write operation timed out。放宽 write/read；connect/pool
# 保持较短以便网络异常时快速失败。
zot.client.timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)


# ---------------------------------------------------------------------------
# v1.8.0 — Domain hard-mapping & helpers
# ---------------------------------------------------------------------------
# 已知平台域名 → 推荐的 Misc 子集合名（短而稳定，方便人工识别）
# - 用作 find_best_collection 的高优先级硬信号
# - 用作 create_misc_subcollection 的命名约定
# - 新增平台时同时加进两个地方（这里 + 文档 SKILL.md 平台映射表）
DOMAIN_TO_SUBCOLL = {
    # 中文平台
    "mp.weixin.qq.com": "wechat",
    "weixin.qq.com": "wechat",
    "chaspark.com": "chaspark",          # 华为背景的茶思屋
    "bilibili.com": "bilibili",
    "xiaohongshu.com": "xhs",            # 小红书
    "zhihu.com": "zhihu",
    "juejin.cn": "juejin",
    # 开发者平台
    "github.com": "github",
    "arxiv.org": "arxiv",
    "ycombinator.com": "hn",
    "news.ycombinator.com": "hn",
    "stackoverflow.com": "stackoverflow",
    "medium.com": "medium",
    "substack.com": "substack",
    # 音视频
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "podcasts.apple.com": "podcast",
    "open.spotify.com": "spotify",
    # 知识/百科
    "wikipedia.org": "wikipedia",
    # Google 系（on-device AI / ML 官方博客集中地）
    "developers.googleblog.com": "google",
    "blog.google": "google",
    "ai.google.dev": "google",
    "cloud.google.com": "google",
    "developers.google.com": "google",
    "research.google": "google",
    "deepmind.google": "google",
    # Microsoft DevBlogs（Raymond Chen 专栏等）
    "devblogs.microsoft.com": "microsoft",
    "learn.microsoft.com": "microsoft",
    "blogs.microsoft.com": "microsoft",
}
# NOTE: personal blogs the maintainer follows are deliberately NOT listed here.
# They are library-specific reading-interest data, so they live in an untracked
# local overlay instead — see _load_domain_overrides(). Add your own there.

# monolith 在这些域名上会挂载成百上千个 .woff2 字体文件，必须加 -F (--no-fonts)。
# 库特定的字体重灾区（如某些 Cloudflare-fronted WordPress 个人博客）同样走 overlay。
_FONT_HEAVY_DOMAINS = (
    "googleblog.com", "blog.google", "ai.google.dev",
    "cloud.google.com", "developers.google.com",
    "research.google", "deepmind.google",
)

# 5 分钟 TTL 缓存 _all_collections() 的结果，避免每次 archive 都全量拉
_collections_cache = {"data": None, "ts": 0.0}
_COLLECTIONS_CACHE_TTL = 300  # seconds

# 5 分钟 TTL 缓存域名 overlay（本地文件很小，但 archive 路径会查它两次）
_domain_overlay_cache = {"data": None, "ts": 0.0}
_DOMAIN_OVERLAY_TTL = 300  # seconds


def _load_domain_overrides(force_refresh=False):
    """读取本地域名 overlay：``{"map": {...}, "no_fonts": [...]}``

    overlay 承载**库特定**的域名知识——用户关注的个人博客、以及在字体重灾区
    需要跳过字体的域名。这类数据属于「阅读兴趣」信号，不能进公共源码树，
    因此外置到未被跟踪的本地文件（``ZOTERO_DOMAIN_MAP``，默认在
    ``ZOTERO_VOCAB_DIR`` 下）。

    文件缺失 / 无权限 / 格式损坏 → 一律退化成空 overlay，**归档绝不因此失败**。
    """
    import time as _time
    now = _time.time()
    if (not force_refresh and _domain_overlay_cache["data"] is not None
            and (now - _domain_overlay_cache["ts"]) < _DOMAIN_OVERLAY_TTL):
        return _domain_overlay_cache["data"]

    overlay = {"map": {}, "no_fonts": []}
    try:
        with open(_get_domain_map_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            mapping = raw.get("map")
            if isinstance(mapping, dict):
                overlay["map"] = {str(k).lower(): str(v)
                                  for k, v in mapping.items() if k and v}
            no_fonts = raw.get("no_fonts")
            if isinstance(no_fonts, list):
                overlay["no_fonts"] = [str(d).lower() for d in no_fonts if d]
    except (OSError, ValueError):
        pass  # 无 overlay → 只用内置表

    _domain_overlay_cache["data"] = overlay
    _domain_overlay_cache["ts"] = now
    return overlay


def _domain_wants_no_fonts(url):
    """该 URL 的域名是否需要 monolith ``-F``（跳过 webfonts）"""
    low = (url or "").lower()
    if any(d in low for d in _FONT_HEAVY_DOMAINS):
        return True
    return any(d in low for d in _load_domain_overrides()["no_fonts"])


def _domain_subcoll_name(url):
    """从 URL 提取已知平台的子集合名。未命中返回 None。

    先查本地 overlay（``_load_domain_overrides()["map"]``），再查内置
    ``DOMAIN_TO_SUBCOLL``。overlay 优先，便于用户覆盖任何域名。

    使用 netloc 后缀匹配，支持子域名（如 news.ycombinator.com → ycombinator.com）。
    要求 '.' 边界，避免 'notgithub.com' 误匹配 'github.com' 这类 substring bug。

    Examples:
        >>> _domain_subcoll_name("https://mp.weixin.qq.com/s/abc?scene=334")
        'wechat'
        >>> _domain_subcoll_name("https://news.ycombinator.com/item?id=12345")
        'hn'
        >>> _domain_subcoll_name("https://www.bilibili.com/video/BV1xx")
        'bilibili'
        >>> _domain_subcoll_name("https://example.com/post/123")
        None
        >>> _domain_subcoll_name("https://notgithub.com/evil")  # substring 假阳性
        None
    """
    if not url:
        return None
    try:
        netloc = urlparse(url.lower()).netloc
    except (ValueError, AttributeError):
        return None
    if not netloc:
        return None
    # 按域名长度降序遍历，更具体的域名先匹配
    # (e.g. "mp.weixin.qq.com" 应在 "weixin.qq.com" 之前命中)
    overlay = dict(DOMAIN_TO_SUBCOLL)
    overlay.update(_load_domain_overrides()["map"])  # overlay 覆盖内置
    for dom, sub in sorted(overlay.items(), key=lambda x: -len(x[0])):
        if netloc == dom or netloc.endswith("." + dom):
            return sub
    return None


def _all_collections(force_refresh=False):
    """分页拉取所有 collections（库大时 zot.collections() 默认 limit=100 会漏掉大部分）

    5 分钟 TTL 缓存。v1.7.4 SKILL.md 已承诺该函数但代码未实现——v1.8.0 落地。
    """
    import time as _time
    now = _time.time()
    if not force_refresh and _collections_cache["data"] is not None and (now - _collections_cache["ts"]) < _COLLECTIONS_CACHE_TTL:
        return _collections_cache["data"]
    # pyzotero everything() 走分页 API
    all_cols = list(zot.everything(zot.collections()))
    _collections_cache["data"] = all_cols
    _collections_cache["ts"] = now
    return all_cols


def _find_existing_domain_collection(url):
    """根据 URL 域名在库内查找已存在的 Misc--<sub> 集合。

    Returns:
        (coll_key, coll_name) 或 None
    """
    sub = _domain_subcoll_name(url)
    if not sub:
        return None
    target_name = f"Misc--{sub}"
    for c in _all_collections():
        if c['data'].get('name') == target_name:
            return c['key'], target_name
    return None


def _is_collection_empty(coll_key):
    """检查 collection 是否真的空（0 items）。

    注意：zot.collection_items() 默认 limit=100，大库会漏判。
    """
    try:
        items = list(zot.everything(zot.collection_items(coll_key)))
        return len(items) == 0
    except Exception:
        return False


def _delete_collection_raw(coll_key):
    """通过 raw Zotero Web API 删除 collection（处理 version header）

    pyzotero.delete_collection() 在 key-only 模式下报
    "string indices must be integers"（参见 2026-07-06 π 事件踩坑）。
    """
    import requests as _requests
    r = _requests.get(
        f"https://api.zotero.org/{LIBRARY_TYPE}s/{LIBRARY_ID}/collections/{coll_key}",
        headers={"Authorization": f"Bearer {API_KEY}", "Zotero-API-Version": "3"},
    )
    if r.status_code != 200:
        return False, f"GET failed: {r.status_code}"
    ver = r.json().get("version") or r.json().get("data", {}).get("version")
    if not ver:
        return False, "no version found"
    r = _requests.delete(
        f"https://api.zotero.org/{LIBRARY_TYPE}s/{LIBRARY_ID}/collections/{coll_key}",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Zotero-API-Version": "3",
            "If-Unmodified-Since-Version": str(ver),
        },
    )
    if r.status_code == 204:
        return True, "deleted"
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def _invalidate_collections_cache():
    """强制刷新 _all_collections() 缓存（写操作后调用）"""
    _collections_cache["data"] = None
    _collections_cache["ts"] = 0.0


# ---------------------------------------------------------------------------
# v1.8.1 — WeChat MP article HTML post-processing
# ---------------------------------------------------------------------------
# 微信公众号文章由 monolith 保存后，正文被 visibility:hidden + opacity:0 隐藏，
# 图片用 data-src 懒加载——两者都依赖 JS。Zotero 禁用 JS，导致正文空白。
# 此函数在 monolith 完成后对 HTML 做后处理，移除反爬样式并展开图片 src。


def _fix_wechat_html(filepath):
    """Post-process monolith-saved WeChat MP HTML for offline/Zotero viewing.

    Fixes JS-dependent issues that break offline rendering in simple WebView
    renderers (Zotero's Qt WebEngine, simplified HTML readers):

      1. ``#js_content`` inline ``visibility: hidden; opacity: 0`` → removed
         (WeChat's default state — JS unhides after font/network readiness check)
      2. ``<img data-src="URL" src="data:image/...base64...">`` → keep the base64
         ``src`` (it's the actual image data, not a placeholder) and strip
         ``data-src`` (lazy-load trigger that's useless offline)
      3. Strip WeChat-specific debugging attributes (data-aistatus, data-imgfileid,
         data-s, data-ratio, data-type, data-w) — these are noise that bloats the
         file by ~8 KB total and serves no offline purpose.

    v1.8.2 fix: previous version replaced the inline ``src="data:image/...base64"``
    with the external ``data-src`` URL, **destroying 19.6 MB of image data** for
    a 20.9 MB article. The base64 src is NOT a placeholder — it's the real image
    data that WeChat inlines so the article can be cached by MP client. Without
    it, offline Zotero rendering shows no images.

    Uses regex-based matching so minor spacing/order variations are tolerated.
    Returns True if any changes were made, False otherwise.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    # Guard: must contain the WeChat article body div
    if 'id="js_content"' not in html:
        return False

    changed = False

    # 1. Unhide the article body — strip visibility:hidden and opacity:0
    #    from the #js_content inline style attribute.
    def _clean_js_content_style(m):
        tag = m.group(0)
        tag = re.sub(r'visibility\s*:\s*hidden\s*;?\s*', '', tag,
                     flags=re.IGNORECASE)
        tag = re.sub(r'opacity\s*:\s*0\s*;?\s*', '', tag,
                     flags=re.IGNORECASE)
        return tag

    html, n1 = re.subn(
        r'<div[^>]*\s+id="js_content"[^>]*style="[^"]*"[^>]*>',
        _clean_js_content_style, html
    )

    # 2. Strip data-src from lazy-loaded images, KEEPING the inline base64 src.
    #    WeChat pages have <img data-src="https://..." src="data:image/...;base64,..."/>
    #    The src is the ACTUAL image data (inlined for offline caching) — NOT a
    #    placeholder. v1.8.1's bug replaced src with data-src URL, losing 19.6MB
    #    of image data. We now just drop the redundant data-src attribute.
    def _strip_data_src(m):
        tag = m.group(0)
        tag = re.sub(r'\s*data-src="[^"]*"', '', tag)
        return tag

    html, n2 = re.subn(
        r'<img[^>]*data-src="https?://[^"]*"[^>]*>',
        _strip_data_src, html
    )

    # 3. Strip WeChat-specific debugging attributes (cosmetic noise, ~8KB savings)
    def _strip_wechat_attrs(m):
        tag = m.group(0)
        for attr in ['data-aistatus', 'data-imgfileid', 'data-s', 'data-ratio',
                     'data-type', 'data-w']:
            tag = re.sub(rf'\s*{attr}="[^"]*"', '', tag)
        return tag

    html, n3 = re.subn(r'<img[^>]*>', _strip_wechat_attrs, html)

    if n1 or n2 or n3:
        changed = True

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html)

    return changed


# Cache forbidden collections (root + all descendants) and items
_forbidden_collection_keys = None
_forbidden_item_keys = None
# Last saved offline file path — consumed by _create_content_note for LLM summarization
_last_offline_file = None

# Sentinel returned by _llm_summarize when the Claude/agent path is taken
# (minis-model-use not available → pending task file written for agent to process)
_LLM_PENDING = object()


def _get_forbidden_collection_keys():
    """Return set of all collection keys under 🙊Personal (including root, recursive).

    Results are cached indefinitely — call _invalidate_forbidden_cache() to refresh.
    """
    global _forbidden_collection_keys
    if _forbidden_collection_keys is not None:
        return _forbidden_collection_keys

    def _get_sub_collections(parent_key):
        subs = [parent_key]
        for c in _all_collections():
            if c['data'].get('parentCollection') == parent_key:
                subs.extend(_get_sub_collections(c['key']))
        return subs

    _forbidden_collection_keys = set(_get_sub_collections(FORBIDDEN_COLLECTION))
    return _forbidden_collection_keys


def _invalidate_forbidden_cache():
    """Invalidate forbidden caches (called after write operations that change collection membership)."""
    global _forbidden_item_keys, _forbidden_collection_keys
    _forbidden_item_keys = None
    _forbidden_collection_keys = None


def get_forbidden_items():
    """Get all item keys in 🙊Personal collection (recursively)"""
    global _forbidden_item_keys
    if _forbidden_item_keys is not None:
        return _forbidden_item_keys

    forbidden = set()
    forbidden_collections = _get_forbidden_collection_keys()

    # Get all items in these collections
    for coll_key in forbidden_collections:
        items = zot.collection_items(coll_key)
        for item in items:
            forbidden.add(item['key'])
            # Also add children (notes, attachments)
            children = item.get('children', [])
            for child in children:
                forbidden.add(child['key'])

    _forbidden_item_keys = forbidden
    return forbidden

def is_allowed(item_key):
    """Check if item is NOT in forbidden collection"""
    return item_key not in get_forbidden_items()

def get_collection_map():
    """Build collection key->name mapping (uses _all_collections to avoid limit=100 truncation)"""
    collections = _all_collections()
    return {c['key']: c['data'].get('name', 'Unknown') for c in collections}

def get_item_collections(item_key):
    """Get collection names for an item (uses _all_collections to avoid limit=100 truncation)"""
    collections = _all_collections()
    item_collections = []
    for c in collections:
        coll_key = c['key']
        items = zot.collection_items(coll_key)
        if any(item['key'] == item_key for item in items):
            item_collections.append(c['data'].get('name', 'Unknown'))
    return item_collections

def search(query, limit=10, search_tags=False, search_collections=False):
    """Search items in Zotero library.

    When searching by query text, results are ranked by Zotero's relevance
    algorithm (title/creator match). When searching by collection, items
    are in dateAdded descending order (newest first).
    """
    forbidden = get_forbidden_items()
    
    results = []
    
    if search_collections:
        # Search by collection name (use _all_collections to avoid limit=100 truncation)
        # Word-boundary match: "pi" matches "Misc--pi/π" but NOT "pipeline"
        collections = _all_collections()
        coll_map = get_collection_map()
        for c in collections:
            coll_name = c['data'].get('name', '')
            if re.search(r'\b' + re.escape(query.lower()) + r'\b', coll_name.lower()):
                items = zot.collection_items(c['key'])
                for item in items:
                    if item['key'] not in forbidden:
                        item['_matched_collection'] = coll_name
                        results.append(item)
    
    if search_tags or (not search_collections):
        # Regular search + tag search
        all_items = zot.items(q=query if not search_collections else None, limit=100)
        for item in all_items:
            if item['key'] in forbidden:
                continue
            
            data = item.get('data', {})
            match = False
            
            if not search_tags:
                # Default: search title/creator
                match = True
            else:
                # Tag search only
                tags = [t.get('tag', '') for t in data.get('tags', [])]
                if any(query.lower() in t.lower() for t in tags):
                    match = True
            
            if match and item not in results:
                results.append(item)
    
    # Limit results
    results = results[:limit]
    
    print(f"\n📚 Found {len(results)} items (excluding 🙊Personal):\n")
    for i, item in enumerate(results, 1):
        data = item.get('data', {})
        title = data.get('title', 'No title')
        item_type = data.get('itemType', 'unknown')
        key = item['key']
        
        # Get creators
        creators = data.get('creators', [])
        if creators:
            author_parts = []
            for c in creators[:2]:
                if 'lastName' in c:
                    author_parts.append(c['lastName'])
                elif 'name' in c:
                    author_parts.append(c['name'])
            author = ', '.join(author_parts)
            if len(creators) > 2:
                author += ' et al.'
        else:
            author = 'Unknown'
        
        # Get tags
        tags = [t.get('tag', '') for t in data.get('tags', [])][:3]
        tag_str = f" | Tags: {', '.join(tags)}" if tags else ""
        
        # Collection info
        coll_info = item.get('_matched_collection', '')
        coll_str = f" | 📁 {coll_info}" if coll_info else ""
        
        print(f"{i}. [{item_type}] {title[:70]}")
        print(f"   🔑 {key} | 👤 {author}{tag_str}{coll_str}\n")

def search_by_collection(collection_name):
    """Find collections by name (word-boundary match).

    Shows matching collection keys and item counts. Uses \\b boundary
    so "pi" matches "Misc--pi/π" but NOT "pipeline" in "Misc--CPU/pipeline".

    For browsing items: use ``zot search`` or ``zot list``.
    Uses _all_collections() to avoid the default limit=100 truncation.
    """
    forbidden = get_forbidden_items()
    collections = _all_collections()

    # Find matching collections (word-boundary match)
    query_lower = collection_name.lower()
    matched_colls = []
    for c in collections:
        name = c['data'].get('name', '')
        if re.search(r'\b' + re.escape(query_lower) + r'\b', name.lower()):
            matched_colls.append((c['key'], name))

    if not matched_colls:
        print(f"\n📁 Collection search: '{collection_name}'")
        print("📚 No matching collections found.\n")
        return

    print(f"\n📁 Collection search: '{collection_name}'")
    print(f"📁 Matched {len(matched_colls)} collection(s):\n")
    for ck, name in matched_colls:
        # Item count (respecting forbidden exclusion)
        items = list(zot.collection_items(ck))
        visible = [it for it in items if it['key'] not in forbidden]
        print(f"   • {name}")
        print(f"     🔑 {ck}  |  📦 {len(visible)} items\n")

def search_by_tag(tag, limit=10):
    """Search items by tag using native Zotero API tag filter.

    Uses zot.items(tag=...) which hits the server-side tag index,
    avoiding a full library scan + client-side filter.
    """
    forbidden = get_forbidden_items()
    all_items = zot.items(tag=tag, limit=200)
    
    results = []
    for item in all_items:
        if item['key'] not in forbidden:
            results.append(item)
        if len(results) >= limit:
            break
    
    print(f"\n🏷️ Tag search: '{tag}'")
    print(f"📚 Found {len(results)} items (excluding 🙊Personal):\n")
    
    for i, item in enumerate(results, 1):
        data = item.get('data', {})
        title = data.get('title', 'No title')
        item_type = data.get('itemType', 'unknown')
        key = item['key']
        tags = [t.get('tag', '') for t in data.get('tags', [])][:5]
        print(f"{i}. [{item_type}] {title[:60]}...")
        print(f"   🔑 {key} | 🏷️ {', '.join(tags)}\n")

def list_items(limit=10):
    """List recent items in dateAdded descending order (newest first).

    Uses Zotero API default sort which matches the client's "Date Added" column.
    """
    forbidden = get_forbidden_items()
    all_items = zot.items(limit=50)
    
    results = [item for item in all_items if item['key'] not in forbidden][:limit]
    
    print(f"\n📚 Recent {len(results)} items (excluding 🙊Personal):\n")
    for i, item in enumerate(results, 1):
        data = item.get('data', {})
        title = data.get('title', 'No title')
        item_type = data.get('itemType', 'unknown')
        key = item['key']
        print(f"{i}. [{item_type}] {title[:60]}... (🔑 {key})")

def list_collections():
    """List all collections (excluding 🙊Personal).

    Uses _all_collections() to avoid the default limit=100 truncation.
    """
    collections = _all_collections()
    forbidden_keys = _get_forbidden_collection_keys()

    print(f"\n📁 Collections (excluding 🙊Personal):\n")
    for c in collections:
        if c['key'] in forbidden_keys:
            continue
            
        name = c['data'].get('name', 'Unknown')
        key = c['key']
        parent = c['data'].get('parentCollection')
        parent_str = f" (parent: {parent})" if parent else ""
        print(f"  • {name}{parent_str}")
        print(f"    Key: {key}\n")

def add_item(item_type, title, url, coll_key, extra_json=None):
    """Add a new item to Zotero with automatic /unread tag

    ``/unread`` 写 ``type: 0``（manual）—— 与 ``_DEFAULT_TAG_TYPE`` 一致，
    避免 CLI 打的 tag 落进 Zotero「可一键清空的自动标签」桶。
    """
    item = {
        'itemType': item_type,
        'title': title,
        'url': url,
        'tags': [{'tag': '/unread', 'type': _DEFAULT_TAG_TYPE}]
    }
    if extra_json:
        import json
        item.update(json.loads(extra_json))
        # Ensure /unread is still present even if extra_json had tags
        tags = item.get('tags', [])
        if not any(t.get('tag') == '/unread' for t in tags):
            tags.append({'tag': '/unread', 'type': _DEFAULT_TAG_TYPE})
            item['tags'] = tags

    response = zot.create_items([item])
    if response.get('successful'):
        item_key = response['successful']['0']['key']
        print(f"✅ Created item: {item_key}")
        # Add to collection
        items = zot.item(item_key)
        fetched = items[0] if isinstance(items, list) else items
        zot.addto_collection(coll_key, fetched)
        print(f"📁 Added to collection: {coll_key}")
        print(f"🏷️  Tagged: /unread")
        return item_key
    else:
        print(f"❌ Failed: {response.get('failed', {})}")
        return None


# ========== 归档工作流 ==========

def _arxiv_abs_url(url):
    """将 arXiv /pdf/ 下载链接改写为 /abs/ 摘要页，作为元数据抓取源。

    PDF 是二进制，没有 <title>/<meta> 可提取；摘要页提供标题 + 完整摘要。
    与 _arxiv_pdf_url()（abs → pdf，供下载附件用）方向相反。
    非 arXiv URL 原样返回。
    """
    m = re.search(r'arxiv\.org/pdf/([^/?]+)', url)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    return url


# 学术预印本站点：netloc 后缀 → Zotero preprint item 的 repository 字段值
_PREPRINT_DOMAINS = (
    ("arxiv.org", "arXiv"),
    ("biorxiv.org", "bioRxiv"),
    ("medrxiv.org", "medRxiv"),
    ("chemrxiv.org", "ChemRxiv"),
    ("researchsquare.com", "Research Square"),
    ("ssrn.com", "SSRN"),
)


def _preprint_from_url(url):
    """学术预印本 URL → repository 名（arXiv/bioRxiv/...）；非预印本返回 None。

    使用 netloc 后缀匹配（同 DOMAIN_TO_SUBCOLL 的 _domain_subcoll_name），
    避免 'fakearxiv.org' 这类 substring 假阳性把普通网页误判成论文。

    Returns:
        repository 字符串（如 'arXiv'），或 None
    """
    if not url:
        return None
    try:
        netloc = urlparse(url.lower()).netloc.split(":")[0]
    except (ValueError, AttributeError):
        return None
    if not netloc:
        return None
    for dom, repo in _PREPRINT_DOMAINS:
        if netloc == dom or netloc.endswith("." + dom):
            return repo
    return None


def fetch_url_metadata(url):
    """获取 URL 的标题和描述，支持常见平台特殊处理

    itemType 判定原则（v2.4.0）：**先按 URL 判定**，再抓取内容。
    - 学术预印本 URL（arxiv.org/biorxiv.org/...）→ 直接判定 preprint
    - 这样即使抓取失败（二进制 PDF、反爬、超时），也不会回退成 webpage
      —— 修复 arxiv.org/pdf/* 等二进制论文链接被归档为 webpage 的问题。
    """
    # ---- 预印本 URL：按 URL 判定 itemType（不依赖抓取成功）----
    preprint_repo = _preprint_from_url(url)
    if preprint_repo:
        item_type = "preprint"
        extra_fields = {"repository": preprint_repo}
    else:
        item_type = "webpage"
        extra_fields = {}

    # Apple Podcasts: 用 iTunes API
    if "podcasts.apple.com" in url:
        m = re.search(r'i=(\d+)', url)
        if m:
            episode_id = m.group(1)
            try:
                coll_m = re.search(r'id(\d+)', url)
                coll_id = coll_m.group(1) if coll_m else "1434243584"
                api_url = f"https://itunes.apple.com/lookup?id={coll_id}&media=podcast&entity=podcastEpisode&limit=200"
                result = subprocess.run(
                    ["curl", "-s", api_url],
                    capture_output=True, text=True, timeout=15
                )
                data = json.loads(result.stdout)
                for r in data.get("results", []):
                    if str(r.get("trackId")) == episode_id:
                        return {
                            "title": r.get("trackName", ""),
                            "description": r.get("description", "")[:500],
                            "itemType": "podcast",
                            "seriesTitle": r.get("collectionName", "")
                        }
            except Exception as e:
                print(f"Apple Podcasts API error: {e}")

    # arXiv PDF 是二进制：元数据抓取源改写为摘要页，附件下载仍用原 URL（PDF）
    fetch_url = _arxiv_abs_url(url)

    # 通用网页抓取（bytes 模式，二进制内容不再抛 UnicodeDecodeError）
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-A", "Mozilla/5.0", "--max-time", "15", fetch_url],
            capture_output=True, timeout=20
        )
        html = result.stdout.decode("utf-8", errors="replace")
        if result.returncode != 0 or not html.strip():
            return {"title": url, "description": "", "itemType": item_type,
                    "error": f"curl failed rc={result.returncode}", **extra_fields}

        # 非 HTML（二进制 PDF 等）不做文本解析，标题保持 URL，itemType 保持 URL 判定值
        if "<title" not in html and "<meta" not in html and "<html" not in html.lower():
            return {"title": url, "description": "", "itemType": item_type,
                    "error": "not an HTML page", **extra_fields}

        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else url
        # arXiv abs 页 <title> 形如 "[2608.20711] AsmEvo: ..." → 去掉 "[id] " 前缀
        if re.search(r'arxiv\.org/abs/', fetch_url):
            title = re.sub(r'^\[\d{4}\.\d{4,5}\]\s*', '', title)

        # 描述优先级：citation_abstract（学术页全量摘要）> description > og:description
        description = ""
        for meta_name in ("citation_abstract", "description", "og:description"):
            desc_match = re.search(
                r'<meta[^>]+(?:name|property)=["\']' + re.escape(meta_name) +
                r'["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE
            )
            if desc_match:
                description = desc_match.group(1).strip()[:3000]
                break

        # 预印本已在 URL 阶段判定；仅 webpage 允许按内容升级为 podcast
        if item_type == "webpage" and re.search(r'podcast|episode|播客', title + description, re.I):
            item_type = "podcast"
        return {"title": title, "description": description, "itemType": item_type, **extra_fields}
    except Exception as e:
        return {"title": url, "description": "", "itemType": item_type,
                "error": str(e), **extra_fields}


# ---------------------------------------------------------------------------
# v2.5.0 — Library-native tag vocabulary
# ---------------------------------------------------------------------------
# 设计约束：**源码里不出现任何具体 tag 字面量**。本仓库是公共仓库，真实 tag
# 名属于用户私人数据。一切 tag 名只在运行时从 API 派生，落盘到仓库之外的
# ZOTERO_VOCAB_DIR。唯一例外 `/unread`（既有公共约定）。
#
# 旧实现 get_existing_tags() 用 zot.tags(limit=200)，而 /tags 默认按 numItems
# **升序**排序 —— 它拿到的是全库最冷门的 200 个 tag，与真实高频 tag 交集为 0。
# 这才是「tag 发散、复用从未生效」的根因。

_VOCAB_CACHE_TTL = 86400          # 24h —— tag 词表变化很慢
_vocab_cache = {"data": None, "ts": 0.0}

# 状态 tag：/unread /reading /done 参与「已存在」判定，但不参与主题匹配
_STATUS_TAG_SLUGS = frozenset({"unread", "reading", "done"})

# root 候选最低分。低于此值说明库里没有合适主题 → 走「新建 root」路径
_ROOT_SCORE_THRESHOLD = 2.0

# 「仅 automatic」tag 的降权系数。导入时抓来的元数据垃圾（英文短语式，
# 形如 "Computer Science"）只以 automatic 存在，而用户策展的词汇表全是 manual。
W_AUTO_ONLY = 0.5

# 写入新 tag 时用的 type：0 = manual。1 = automatic 会在 Zotero 标签选择器里
# 落进可隐藏 / 可被「Delete Automatic Tags」一键清空的桶（不可撤销）。
_DEFAULT_TAG_TYPE = 0


def _vocab_path():
    return os.path.join(_get_vocab_dir(), "tags.json")


def _pairs_path():
    return os.path.join(_get_vocab_dir(), "pairs.json")


def _empty_vocab(reason="empty"):
    return {"roots": [], "children": {}, "orphans": [], "pairs": {},
            "local_new": [], "generated_ts": 0.0, "count": 0,
            "source": reason}


# ── 命名法工具层 ──────────────────────────────────────────────────────────
# /rootEmoji          → level-1 root（结尾带 emoji）
# #root-child[-leaf]  → 层级子标签
# /unread /reading /done → 状态 tag，不参与主题匹配

# 三个不落在 S*/M* 判据内、但属于 emoji 的码位
ZWJ = "\u200d"        # zero-width joiner（Cf）
VS16 = "\ufe0f"       # variation selector-16（Mn）
KEYCAP = "\u20e3"     # combining enclosing keycap（Me）


def _strip_tag_emoji(tag):
    """剥离 tag 尾部的 emoji / 修饰符。

    emoji 的 Unicode 类别是 S*（符号）或 M*（修饰符），但有三类**不是**，
    必须显式列出，否则复合 emoji 剥不干净（见文件顶部的 ZWJ / VS16 / KEYCAP）：

    - U+200D ZWJ（类别 Cf）—— 拼接复合 emoji
    - U+FE0F 变体选择符（类别 Mn）
    - U+20E3 键帽（类别 Me）

    只从**尾部**剥，避免破坏 tag 中段的连字符结构。
    """
    import unicodedata
    s = (tag or "").rstrip()
    while s:
        c = s[-1]
        if (unicodedata.category(c)[0] in ("S", "M")
                or c in (ZWJ, VS16, KEYCAP)):
            s = s[:-1]
            continue
        break
    return s


def _root_slug(tag):
    """``/rootEmoji`` → ``root``（casefold）。非 root 形态返回 ''"""
    if not (tag or "").startswith("/"):
        return ""
    return _strip_tag_emoji(tag[1:]).strip().casefold()


def _child_body(tag):
    """``#root-child🧇`` → ``root-child``（去 sigil / 去 emoji / casefold）"""
    if not (tag or "").startswith("#"):
        return ""
    return _strip_tag_emoji(tag[1:]).strip().casefold()


def _child_root_slug(tag):
    """``#root-child`` → ``root``（以**第一个** '-' 切分）

    仅在 root 未知时作为兜底归属；已知 root 集合时应改用最长 root slug 前缀
    匹配（见 _vocab_add_tag），因为 root 名本身可能含连字符。
    """
    body = _child_body(tag)
    return body.split("-", 1)[0] if body else ""


def _child_slug(tag):
    """``#root-child-leaf`` → ``child-leaf``（去掉 root 段；casefold）"""
    body = _child_body(tag)
    return body.split("-", 1)[1] if "-" in body else ""


def _tag_key(tag):
    """去 sigil / 去 emoji / casefold —— 去重键。

    使 ``#demo-alpha`` 与 ``#demo-alpha🧇`` 视为同一个 tag。
    """
    t = (tag or "").strip()
    if t[:1] in ("#", "/"):
        t = t[1:]
    return _strip_tag_emoji(t).strip().casefold()


def _norm_child_slug(child_slug):
    """归一化 child slug 用于查重：取最后一段 + 去复数"""
    seg = (child_slug or "").rsplit("-", 1)[-1]
    if len(seg) > 3 and seg.endswith("s") and not seg.endswith("ss"):
        seg = seg[:-1]
    return seg


# ── 词表拉取 / 解析 / 缓存 ─────────────────────────────────────────────────

def _fetch_all_tags_raw(min_count=3, max_pages=12, page_size=100):
    """裸 HTTP 分页拉取 tag 及其计数。

    pyzotero 的 retrieve 装饰器会把任何含 "tags" 的 URL 经 _tags_data() 拍平成
    字符串名，numItems 永远拿不到 —— 所以计数只能走裸 HTTP。

    显式 ``sort=numItems&direction=desc``：这才是那份「高频 tag 表」，
    而且是派生的、永远新鲜的，不需要手工维护。

    注意：numItems 排序下 ``Total-Results`` 响应头不可信（实测恒为 1），
    因此终止条件用「本页最后一条 numItems < min_count」或空页。
    """
    url = f"{zot.endpoint}/{zot.library_type}/{zot.library_id}/tags"
    out, start = [], 0
    for _ in range(max_pages):
        resp = zot.client.get(url, params={
            "sort": "numItems", "direction": "desc",
            "limit": page_size, "start": start,
        })
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        out.extend(page)
        if page[-1].get("meta", {}).get("numItems", 0) < min_count:
            break
        start += page_size
    return out


def _vocab_add_tag(vocab, tag, n=1, types=None):
    """把一个 tag 插进词表结构（幂等）。返回 True 表示新增。

    child → root 归属按 **root slug 最长优先**，与 DOMAIN_TO_SUBCOLL 的 netloc
    后缀规则同源；合法边界是 ``body == slug`` 或 ``body.startswith(slug + '-')``。
    """
    if not tag or " " in tag:
        return False
    types = set(types or [0])
    if tag.startswith("/"):
        slug = _root_slug(tag)
        if not slug or any(r["slug"] == slug for r in vocab["roots"]):
            return False
        vocab["roots"].append({
            "tag": tag, "slug": slug, "n": n,
            "types": sorted(types), "type": 0 if 0 in types else 1,
            "status": slug in _STATUS_TAG_SLUGS, "children_count": 0,
        })
        return True
    if not tag.startswith("#"):
        return False
    body = _child_body(tag)
    if not body:
        return False
    entry = {"tag": tag, "slug": body, "n": n,
             "types": sorted(types), "type": 0 if 0 in types else 1}
    root_slugs = sorted((r["slug"] for r in vocab["roots"]), key=len, reverse=True)
    owner = next((rs for rs in root_slugs
                  if body == rs or body.startswith(rs + "-")), None)
    if owner is None:
        if any(o["tag"] == tag for o in vocab["orphans"]):
            return False
        vocab["orphans"].append(entry)
        return True
    bucket = vocab["children"].setdefault(owner, [])
    if any(c["tag"] == tag for c in bucket):
        return False
    child_slug = body[len(owner):].lstrip("-")
    bucket.append({**entry, "child": child_slug,
                   "norm": _norm_child_slug(child_slug)})
    bucket.sort(key=lambda x: (-x["n"], x["slug"]))
    root = next(r for r in vocab["roots"] if r["slug"] == owner)
    root["children_count"] = len(bucket)
    return True


def _parse_vocab(raw):
    """把 /tags 原始响应解析成词表结构。

    同名 tag 会以 type 0（manual）和 type 1（automatic）两条独立行出现，
    需按名合并计数。分页在 numItems 相同的 tie 上可能重复返回同一行，
    故先按 ``(name, type)`` 取 max（避免重复页把计数翻倍），再跨 type 求和。
    """
    buckets = {}   # (name, type) -> max numItems
    for entry in raw or []:
        name = (entry.get("tag") or "").strip()
        # 带空格的 tag 一律丢弃（既有约定；也顺带排除导入抓来的英文短语垃圾）
        if not name or " " in name:
            continue
        meta = entry.get("meta") or {}
        try:
            n = int(meta.get("numItems") or 0)
        except (TypeError, ValueError):
            n = 0
        try:
            t = int(meta.get("type", 0))
        except (TypeError, ValueError):
            t = 0
        key = (name, t)
        buckets[key] = max(buckets.get(key, 0), n)

    merged = {}
    for (name, t), n in buckets.items():
        rec = merged.setdefault(name, {"n": 0, "types": set()})
        rec["n"] += n
        rec["types"].add(t)

    vocab = _empty_vocab("api")
    # 先插 root，再插 child —— child 归属需要完整的 root 集合
    for name in sorted(merged):
        if name.startswith("/"):
            _vocab_add_tag(vocab, name, merged[name]["n"], merged[name]["types"])
    for name in sorted(merged):
        if name.startswith("#"):
            _vocab_add_tag(vocab, name, merged[name]["n"], merged[name]["types"])

    vocab["roots"].sort(key=lambda r: (-r["n"], r["slug"]))
    vocab["count"] = len(merged)
    return vocab


def _read_vocab_file():
    try:
        with open(_vocab_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_vocab_file(vocab):
    """原子写（tmp + replace），失败静默 —— 词表落盘失败不该影响归档"""
    try:
        os.makedirs(_get_vocab_dir(), exist_ok=True)
        tmp = _vocab_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(vocab, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _vocab_path())
        return True
    except OSError:
        return False


def _load_pairs():
    """中英配对表 —— 只存在于本地（``<ZOTERO_VOCAB_DIR>/pairs.json``）。

    文件格式（两种都接受）::

        {"pairs": [{"en": "demo-alpha", "zh": "demo-阿尔法"}]}
        [{"en": "demo-alpha", "zh": "demo-阿尔法"}]

    返回双向映射 ``{slug: {"slug": 对方 slug, "lang": 对方语言}}``。

    这是配对知识的**唯一**来源 —— 源码里不留任何 seed，因为「哪个概念有中英
    两版」天然是库特定的，写进公共仓库就是泄漏。
    """
    try:
        with open(_pairs_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    items = raw.get("pairs") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return {}
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        en = str(it.get("en") or "").strip().casefold()
        zh = str(it.get("zh") or "").strip().casefold()
        if not en or not zh or en == zh:
            continue
        out[en] = {"slug": zh, "lang": "zh"}
        out[zh] = {"slug": en, "lang": "en"}
    return out


def load_vocab(force_refresh=False):
    """加载本库标签词表（roots / children / orphans / pairs）。

    解析顺序：内存 TTL(24h) → 磁盘未过期 → 裸 HTTP 拉取 → 磁盘陈旧仍用 → 空词表。

    **陈旧词表严格优于无词表**：无词表时我们不发明任何 tag（那正是发散的成因），
    而陈旧词表至少还能复用库内既有 tag。
    """
    import time as _time
    now = _time.time()
    if (not force_refresh and _vocab_cache["data"] is not None
            and (now - _vocab_cache["ts"]) < _VOCAB_CACHE_TTL):
        return _vocab_cache["data"]

    disk = None if force_refresh else _read_vocab_file()
    if disk and (now - float(disk.get("generated_ts") or 0)) < _VOCAB_CACHE_TTL:
        disk["source"] = "disk"
        _vocab_cache["data"], _vocab_cache["ts"] = disk, now
        return disk

    try:
        vocab = _parse_vocab(_fetch_all_tags_raw())
        vocab["generated_ts"] = now
        vocab["pairs"] = _load_pairs()
        _write_vocab_file(vocab)
    except Exception as e:                              # noqa: BLE001 — 归档绝不因此失败
        print(f"⚠️  标签词表拉取失败: {e}")
        if disk:
            age_h = (now - float(disk.get("generated_ts") or 0)) / 3600.0
            print(f"⚠️  回退到本地陈旧词表（{age_h:.0f}h 前）")
            disk["source"] = "stale"
            disk.setdefault("pairs", _load_pairs())
            vocab = disk
        else:
            print("⚠️  无本地词表可用 —— 本次只打 /unread，不发明新 tag")
            vocab = _empty_vocab("vocab_unavailable")

    _vocab_cache["data"], _vocab_cache["ts"] = vocab, now
    return vocab


def _invalidate_vocab_cache():
    """让词表在**下一个进程**重新拉取。

    不删文件：磁盘缓存是 API 不可用时的离线兜底，删了会丢。
    把 mtime 设成 epoch 0，下个进程读到就认为过期。
    """
    _vocab_cache["data"] = None
    _vocab_cache["ts"] = 0.0
    try:
        os.utime(_vocab_path(), (0, 0))
    except OSError:
        pass


def _vocab_note_new_tags(names):
    """把本次新发明的 tag 立刻写回磁盘缓存，让**下一次归档就能复用**。

    没有这一步，「复用优先」要等 24h TTL 过期才生效 —— 也就是根本没生效。
    """
    # root 先插：child 的归属需要完整的 root 集合，顺序反了 child 会落成孤儿
    # 且再也不会被重新归属（sorted 稳定，组内原序不变）
    tags = sorted((n for n in (names or []) if n and " " not in n),
                  key=lambda t: 0 if t.startswith("/") else 1)
    if not tags:
        return
    disk = _read_vocab_file()
    if not disk:
        return
    added = [t for t in tags if _vocab_add_tag(disk, t, 1, {0})]
    if not added:
        return
    disk["local_new"] = sorted(set(disk.get("local_new") or []) | set(added))
    disk["count"] = int(disk.get("count") or 0) + len(added)
    if _write_vocab_file(disk):
        _vocab_cache["data"] = None     # 下次从磁盘重读（含新 tag）


def _vocab_tag_types(vocab=None):
    """``{tag 名: type}``，供写入时沿用库内既有 type（不再制造 0/1 双份）"""
    vocab = vocab if vocab is not None else load_vocab()
    out = {}
    for r in vocab.get("roots", []):
        out[r["tag"]] = r.get("type", 0)
    for lst in (vocab.get("children") or {}).values():
        for c in lst:
            out[c["tag"]] = c.get("type", 0)
    for o in vocab.get("orphans", []):
        out[o["tag"]] = o.get("type", 0)
    return out



def _emoji_for_tag(tag_text):
    """根据 tag 内容选择 emoji"""
    text = tag_text.lower()
    emoji_map = [
        # (关键词列表, emoji)
        (["ai", "人工智能", "llm", "大模型", "gpt", "claude", "机器学习"], "🤖"),
        (["经济", "财富", "金融", "投资", "资本", "钱", "economics", "finance", "wealth"], "💰"),
        (["编程", "代码", "开发", "programming", "coding", "developer", "software", "python", "javascript"], "💻"),
        (["数学", "math", "statistics", "概率", "代数", "几何"], "🔢"),
        (["哲学", "philosophy", "逻辑", "logic", "思考", "ethics"], "🤔"),
        (["播客", "podcast", "广播", "audio", "pod"], "🎙️"),
        (["视频", "video", "youtube", "bilibili", "movie"], "📺"),
        (["教程", "入门", "指南", "guide", "tutorial", "how to", "学习"], "📚"),
        (["工具", "工具", "plugin", "extension", "library", "framework", "cli", "app"], "🛠️"),
        (["研究", "论文", "paper", "arxiv", "survey", "review", "学术"], "📄"),
        (["房产", "房子", "买房", "房价", "housing", "real estate"], "🏠"),
        (["健康", "医学", "医疗", "health", "medicine", "medical"], "🏥"),
        (["政治", "社会", "政策", "politics", "society", "government"], "🌍"),
        (["历史", "historical", "history", "过去"], "📜"),
        (["艺术", "设计", "art", "design", "creative", "paint"], "🎨"),
        (["科学", "物理", "化学", "生物", "science", "physics", "biology"], "🔬"),
        (["游戏", "game", "gaming", "娱乐", "play"], "🎮"),
        (["生活", "日常", "lifestyle", "旅行", "food", "cooking"], "🌱"),
        (["写作", "内容", "content", "writing", "blog", "article"], "✍️"),
        (["数据", "统计", "dataset", "data", "analytics", "visualization"], "📊"),
        (["音乐", "音频", "music", "sound", "song"], "🎵"),
        (["图像", "视觉", "image", "photo", "graphics", "vision"], "🖼️"),
        (["安全", "隐私", "security", "privacy", "cryptography", "加密"], "🔒"),
        (["网络", "互联网", "web", "internet", "network", "cloud"], "🌐"),
        (["商业", "创业", "business", "startup", "company", "marketing"], "💼"),
        (["文学", "小说", "literature", "novel", "fiction", "poem"], "📖"),
        (["宗教", "信仰", "religion", "faith", "spiritual"], "🙏"),
        (["体育", "运动", "sports", "fitness", "exercise"], "⚽"),
        (["法律", "法规", "law", "legal", "policy", "regulation"], "⚖️"),
    ]
    for keywords, emoji in emoji_map:
        if any(kw in text for kw in keywords):
            return emoji
    return "🔗"


# ── 匹配器 ────────────────────────────────────────────────────────────────
#
# 设计原则：**先看库里有什么，再看文本能命中什么**。旧实现反了过来 —— 先由
# 关键词表造 tag 字面量，再去库里找同名，于是永远找不到，每次归档都新建。

# 概念 → (规范 slug, emoji)。**不含任何 tag 字面量**：slug 只有在库里真的存在
# 同名 root 时才被复用，否则只作为「新建 root 的建议名」。
#
# 一举两用：① 别名桥（标题写「数学」也能命中库里 slug 为 math 的 root，尽管
# 文本里没有 "math"）；② 新建 root 时的规范 slug 与 emoji。
#
# 别名取舍：宁可漏（漏了退化成新建 root，是软失败），不可错（错了会打错 tag）。
# 尤其中文别名 —— 它是**子串**匹配，所以剔除了会嵌进其它领域词的两字别名
# （如 tutorial 的「学习」会命中「机器学习」，art 的「设计」会命中「设计模式」）。
_TOPIC_KEYWORDS = [
    (("ai", "artificial intelligence", "llm", "gpt", "chatgpt", "claude",
      "machine learning", "deep learning", "transformer", "neural network",
      "大模型", "机器学习", "深度学习", "神经网络", "人工智能"), "ai", "🤖"),
    (("programming", "code", "coding", "developer", "software", "编程",
      "代码", "开发者", "软件工程"), "programming", "💻"),
    (("economics", "finance", "investment", "wealth", "经济", "金融",
      "投资", "财富", "通胀"), "economics", "💰"),
    (("mathematics", "math", "proof", "theorem", "algebra", "axiom",
      "数学", "证明", "代数", "几何", "拓扑"), "math", "🔢"),
    (("philosophy", "ethics", "metaphysics", "epistemology", "哲学",
      "伦理", "形而上学", "认识论"), "philosophy", "🤔"),
    (("podcast", "episode", "播客", "访谈"), "podcast", "🎙️"),
    (("video", "lecture", "youtube", "bilibili", "视频", "讲座"), "video", "📺"),
    (("tutorial", "guide", "how to", "cheat sheet", "教程", "入门"), "tutorial", "📚"),
    (("cli", "plugin", "extension", "framework", "工具", "插件", "框架"), "tool", "🛠️"),
    (("paper", "preprint", "research", "survey", "arxiv", "academic",
      "论文", "预印本", "综述", "学术"), "paper", "📄"),
    (("book", "reading", "literature", "novel", "书籍", "阅读", "文学",
      "小说"), "book", "📖"),
    (("history", "historical", "ancient", "历史", "古代", "考古"), "history", "📜"),
    (("science", "physics", "chemistry", "biology", "科学", "物理",
      "化学", "生物"), "science", "🔬"),
    (("health", "medicine", "medical", "healthcare", "健康", "医学",
      "医疗"), "health", "🏥"),
    (("politics", "policy", "government", "society", "政治", "政策",
      "政府", "社会"), "politics", "🌍"),
    (("art", "creative", "paint", "艺术", "绘画", "摄影"), "art", "🎨"),
    (("gaming", "game", "娱乐", "游戏"), "game", "🎮"),
    (("data", "dataset", "statistics", "analytics", "visualization",
      "数据", "统计", "可视化"), "data", "📊"),
    (("music", "audio", "sound", "音乐", "歌曲", "音频"), "music", "🎵"),
    (("image", "photo", "vision", "graphics", "图像", "视觉", "图片"), "image", "🖼️"),
    (("security", "privacy", "cryptography", "安全", "隐私", "加密",
      "密码学"), "security", "🔒"),
    (("web", "internet", "cloud", "http", "网络", "互联网", "浏览器"), "web", "🌐"),
    (("business", "startup", "company", "marketing", "商业", "创业",
      "公司", "营销"), "business", "💼"),
    (("housing", "real estate", "mortgage", "房产", "买房", "房贷"), "housing", "🏠"),
    (("writing", "blog", "写作", "博客", "自媒体"), "writing", "✍️"),
    (("life", "lifestyle", "travel", "food", "生活", "旅行", "美食"), "life", "🌱"),
]

# 通用技术缩写 ↔ 展开写法。只收**通用**缩写，不得从真实库内容派生 ——
# 没有这张表，`#xxx-cv` 这类缩写 slug 永远匹配不上 "computer vision"。
_CHILD_SLUG_ALIASES = {
    "cv": ("computer vision", "计算机视觉"),
    "ml": ("machine learning", "机器学习"),
    "llm": ("large language model", "large language models", "大语言模型"),
    "nlp": ("natural language processing", "自然语言处理"),
    "rl": ("reinforcement learning", "强化学习"),
    "fp": ("floating point", "浮点"),
    "gpu": ("graphics processing unit", "显卡"),
    "cpu": ("central processing unit", "处理器"),
    "os": ("operating system", "操作系统"),
    "db": ("database", "数据库"),
    "sql": ("structured query language",),
    "k8s": ("kubernetes",),
    "ci": ("continuous integration", "持续集成"),
    "api": ("application programming interface",),
    "simd": ("vectorization", "vectorisation", "向量化"),
    "jit": ("just in time", "即时编译"),
    "gc": ("garbage collection", "垃圾回收"),
    "vm": ("virtual machine", "虚拟机"),
    "dns": ("domain name system",),
    "tls": ("transport layer security",),
    "json": ("javascript object notation",),
}

# 标题分词停用词
_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "these",
    "those", "it", "its", "by", "as", "from", "at", "up", "out", "about",
    "into", "over", "what", "which", "who", "when", "where", "why", "how",
    "all", "some", "any", "each", "new", "old", "best", "first", "last",
    "such", "do", "does", "did", "can", "could", "will", "would", "should",
    "may", "might", "must", "not", "no", "you", "your", "we", "our",
    "but", "if", "than", "then", "there", "here", "so", "just", "very",
    "more", "most", "other", "only", "also", "one", "get", "got", "make",
    "made", "use", "using", "used", "way", "part", "upon",
})

_MAX_NEEDS = 3        # 每次归档最多提几个「待翻译」概念，避免刷屏
_MAX_CHILDREN = 7     # 1 root + ≤7 children


def _build_slug_aliases():
    """``{slug: frozenset(等价写法)}`` —— 缩写 ↔ 展开、英文 slug ↔ 中文词"""
    out = {}
    for keywords, slug, _emoji in _TOPIC_KEYWORDS:
        out.setdefault(slug, set()).update(k.casefold() for k in keywords)
    for abbr, expansions in _CHILD_SLUG_ALIASES.items():
        out.setdefault(abbr, set()).add(abbr)
        for e in expansions:
            out.setdefault(abbr, set()).add(e.casefold())
            out.setdefault(e.casefold(), set()).add(abbr)
    return {k: frozenset(v) for k, v in out.items()}


_SLUG_ALIASES = _build_slug_aliases()


def _has_cjk(s):
    return bool(re.search(r"[㐀-䶿一-鿿豈-﫿]", s or ""))


def _strip_diacritics(s):
    """去除组合附加符：``Gödel`` → ``Godel``

    否则 ``[A-Za-z]`` 正则会把它拆成 ``Godel`` + 落单的 ``o``。
    """
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s or "")
                   if not unicodedata.combining(c))


def _title_keywords(text):
    """文本 → 有意义的关键词集合（casefold、去停用词、长度 ≥ 2）"""
    words = re.findall(r"[A-Za-z一-鿿]+", _strip_diacritics(text))
    return {w.casefold() for w in words
            if len(w) >= 2 and w.casefold() not in _STOP_WORDS}


def _phrase_in(needle, haystack):
    """needle 是否作为**短语**出现在 haystack 中。

    ASCII 要求词边界（否则 ``os`` 会命中 ``cost``）；CJK 无空格，直接子串匹配。
    """
    needle = (needle or "").casefold()
    if not needle:
        return False
    if _has_cjk(needle):
        return needle in haystack
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack) is not None


def _prior_multiplier(n, auto_only=False):
    """热度先验 —— **乘法**系数，只用来给已有文本证据的 tag 打破平局。

    乘法而非加法：加法会把零文本证据的高频 tag 顶上来，那是「高频霸榜」
    而不是「高频优先」。

    库内「仅 automatic」的 tag 混着导入时抓来的元数据垃圾（英文短语式），
    而用户策展的词汇表全是 manual，故对 auto-only 追加降权。不直接排除，
    以免误伤。
    """
    import math
    prior = min(math.log10(1 + max(0, int(n or 0))), 3.0) / 3.0
    mult = 1.0 + 0.30 * prior
    return mult * W_AUTO_ONLY if auto_only else mult


def _slug_strength(slug, text_lower, words):
    """slug 与文本的匹配强度（未乘热度先验）。0 表示无证据。

    4.0 = 整条 slug 作为短语命中 / 2.5 = 别名桥（中英、缩写）/
    2.0 = 某一段是文本里的完整词 / 1.2 = 某一段是文本词的前缀（≥4 字符）
    """
    if _phrase_in(slug, text_lower):
        return 4.0
    for alias in _SLUG_ALIASES.get(slug, ()):
        if alias != slug and _phrase_in(alias, text_lower):
            return 2.5
    segs = [s for s in slug.replace("_", "-").split("-") if s]
    if any(s in words for s in segs):
        return 2.0
    if any(len(s) >= 4 and any(w.startswith(s) for w in words) for s in segs):
        return 1.2
    return 0.0


def _is_auto_only(entry):
    """该 tag 是否**只**以 automatic 形式存在"""
    return set(entry.get("types") or []) == {1}


def _find_root(vocab, slug):
    for r in vocab.get("roots", []):
        if r["slug"] == slug:
            return r
    return None


def _iter_vocab_candidates(vocab):
    """词表 → 可参与打分的候选（状态 root 及其 children 直接排除）"""
    for r in vocab.get("roots", []):
        if r.get("status"):
            continue
        yield {"kind": "root", "tag": r["tag"], "slug": r["slug"],
               "n": r.get("n", 0), "auto_only": _is_auto_only(r)}
    for root_slug, lst in (vocab.get("children") or {}).items():
        root = _find_root(vocab, root_slug)
        if root is None or root.get("status"):
            continue
        for c in lst:
            yield {"kind": "child", "tag": c["tag"],
                   "slug": c.get("child") or c.get("slug") or "",
                   "root": root_slug, "n": c.get("n", 0),
                   "auto_only": _is_auto_only(c)}


def _parse_tag_hints(tag_hints):
    """用户提示 → ``{"root": tag|None, "children": [tag], "bare": [word]}``

    ``/xxx`` → root 提示；``#xxx`` → 原样保留的 child 提示；裸词 → 待限定
    （会补成 ``#<root>-<word>``，避免用户随手一个词就落成无归属 orphan）。
    """
    root, children, bare = None, [], []
    for h in tag_hints or []:
        h = (h or "").strip()
        if not h:
            continue
        if h.startswith("/"):
            if root is None:
                root = h
        elif h.startswith("#"):
            children.append(h)
        else:
            bare.append(h)
    return {"root": root, "children": children, "bare": bare}


def _root_or_new(vocab, slug, emoji, note):
    """决定 ``/slugEmoji`` —— 但**先看这个 slug 是否已被现有 root 占用**。

    emoji 不同不构成不同的 root：库内已有 ``/demo📦`` 时再造一个 ``/demo🔗``
    就是 tag 发散。状态 slug（``unread``/``reading``/``done``）永不作为主题
    root，直接放弃该候选。
    """
    if slug in _STATUS_TAG_SLUGS:
        return None, None, "none"
    existing = _find_root(vocab, slug) if vocab else None
    if existing and not existing.get("status"):
        return existing["tag"], existing["slug"], "existing"
    return f"/{slug}{emoji}", slug, note


def _new_root_for(text_lower, title, vocab=None):
    """库里没有合适 root 时决定 root。

    slug 取 ``_TOPIC_KEYWORDS`` 的规范 slug（小写连字符），emoji 沿用既有
    ``_emoji_for_tag``。返回 ``(tag, slug, note)``。
    """
    for keywords, slug, emoji in _TOPIC_KEYWORDS:
        if any(_phrase_in(k, text_lower) for k in keywords):
            tag, s, note = _root_or_new(vocab, slug, emoji, "new-topic")
            if s:
                return tag, s, note
    words = re.findall(r"[A-Za-z一-鿿]+", _strip_diacritics(title or ""))
    for w in words:
        core = w.casefold()
        if len(core) < 2 or core in _STOP_WORDS:
            continue
        tag, s, note = _root_or_new(vocab, core, _emoji_for_tag(core),
                                    "new-fallback")
        if s:
            return tag, s, note
    return None, None, "none"


def _child_lang(slug):
    return "zh" if _has_cjk(slug) else "en"


def infer_tags_structured(title, description="", tag_hints=None, vocab=None):
    """推断标签方案 —— **复用优先**，确定性实现（不走 LLM）。

    返回::

        {"root": tag|None, "children": [tag], "new": [tag],
         "needs": [{concept, have, have_lang, want_lang, want_example}],
         "attached": [tag], "mode": str, "scores": {slug: float}}

    ``new`` 是本次要新建的 tag（既有库内 tag 不算）；``needs`` 是需要 agent
    补翻译的中英配对缺口；``attached`` 是命中的既有 tag。

    形态不变量由 ``_merge_tag_plan()`` 单点保证。
    """
    vocab = vocab if vocab is not None else load_vocab()

    # 无词表 → **绝不发明 tag**。无词表还发明正是发散的成因。
    if vocab.get("source") == "vocab_unavailable":
        return {"root": None, "children": [], "new": [], "needs": [],
                "attached": [], "mode": "vocab_unavailable", "scores": {}}

    title = title or ""
    description = description or ""
    if _is_url(title.strip()) and not description.strip():
        return {"root": None, "children": [], "new": [], "needs": [],
                "attached": [], "mode": "url_only", "scores": {}}

    text_lower = _strip_diacritics(f"{title} {description}").casefold()
    words = _title_keywords(f"{title} {description}")
    hints = _parse_tag_hints(tag_hints)

    # ── 打分：children 的得分累加到各自 root ────────────────────────────
    # 某个具体 child 命中，比 root 自己那个泛化 slug 命中强得多。
    root_scores, child_hits = {}, []
    for cand in _iter_vocab_candidates(vocab):
        strength = _slug_strength(cand["slug"], text_lower, words)
        if not strength:
            continue
        score = strength * _prior_multiplier(cand["n"], cand["auto_only"])
        if cand["kind"] == "root":
            root_scores[cand["slug"]] = root_scores.get(cand["slug"], 0.0) + score
        else:
            key = cand["root"]
            root_scores[key] = root_scores.get(key, 0.0) + score
            child_hits.append((score, cand))

    # ── root 选择 ───────────────────────────────────────────────────────
    root_tag, root_slug, root_note = None, None, ""
    if hints["root"]:
        slug = _tag_key(hints["root"])
        existing = _find_root(vocab, slug)
        if existing:
            root_tag, root_slug, root_note = existing["tag"], existing["slug"], "user"
        else:
            root_tag, root_slug, root_note = hints["root"], slug, "user-new"
    elif root_scores:
        best_slug, best_score = max(root_scores.items(),
                                    key=lambda kv: (kv[1], kv[0]))
        if best_score >= _ROOT_SCORE_THRESHOLD:
            r = _find_root(vocab, best_slug)
            if r:
                root_tag, root_slug, root_note = r["tag"], r["slug"], "existing"
    if root_slug is None:
        root_tag, root_slug, root_note = _new_root_for(text_lower, title, vocab)

    # ── children 选择 ───────────────────────────────────────────────────
    root_children = ((vocab.get("children") or {}).get(root_slug, [])
                     if root_slug else [])
    by_child_slug = {c.get("child", "").casefold(): c["tag"] for c in root_children}
    pairs = vocab.get("pairs") or {}

    child_hits.sort(key=lambda x: (-x[0], -x[1]["n"], x[1]["slug"]))
    inferred, seen = [], {_tag_key(root_tag or "")}

    def _push(tag):
        key = _tag_key(tag)
        if not tag or key in seen:
            return False
        seen.add(key)
        inferred.append(tag)
        return True

    attached, needs = [], []
    for _score, cand in child_hits:
        if len(inferred) >= _MAX_CHILDREN:
            break
        if cand.get("root") != root_slug:
            continue                      # 只收本 root 下的 child
        if not _push(cand["tag"]):
            continue
        attached.append(cand["tag"])
        # 中英成对：已知配对**且配对 tag 确实存在于库内**才双语都打，
        # 绝不凭空造一个库里没有的 tag。
        counterpart = pairs.get(cand["slug"]) or {}
        pair_tag = by_child_slug.get((counterpart.get("slug") or "").casefold())
        if pair_tag:
            _push(pair_tag)
            attached.append(pair_tag)

    # 待翻译缺口：只对「还没登记配对」的概念提，且限量
    if root_slug:
        for tag in inferred:
            if len(needs) >= _MAX_NEEDS:
                break
            prefix = "#" + root_slug
            slug = tag[len(prefix):].lstrip("-") if tag.startswith(prefix) else ""
            if not slug or slug.casefold() in pairs:
                continue
            lang = _child_lang(slug)
            needs.append({
                "concept": slug,
                "have": tag,
                "have_lang": lang,
                "want_lang": "en" if lang == "zh" else "zh",
                "want_example": f"#{root_slug}-<{('en' if lang == 'zh' else 'zh')} slug>",
            })

    # 新建 root：顺手把文本里其它概念作为 children 建议交给 agent
    if root_note in ("new-topic", "new-fallback", "user-new"):
        suggestions = []
        for keywords, slug, _emoji in _TOPIC_KEYWORDS:
            if slug == root_slug or len(suggestions) >= _MAX_NEEDS:
                continue
            if any(_phrase_in(k, text_lower) for k in keywords):
                suggestions.append({
                    "concept": slug,
                    "have": None,
                    "have_lang": None,
                    "want_lang": "zh+en",
                    "want_example": f"#{root_slug}-<slug>",
                })
        needs = (needs + suggestions)[:_MAX_NEEDS]

    plan = {"root": root_tag, "root_slug": root_slug, "children": inferred,
            "needs": needs, "attached": attached, "mode": root_note or "none",
            "scores": root_scores}
    plan["new"] = [t for t in ([root_tag] if root_tag else []) + inferred
                   if t not in attached]
    return plan


def _merge_tag_plan(plan, tag_hints=None, max_children=_MAX_CHILDREN):
    """单点保证输出形态：1 root + ≤max_children children，去重，child ≠ root。

    用户 ``#tag`` 提示**优先占 children 席位**且原样保留（显式意图是权威的，
    也是「我就是想打这个 tag」的逃生口）；裸词提示补成 ``#<root>-<word>``。
    """
    plan = dict(plan or {})
    root = plan.get("root")
    # 用 slug 而非 tag 拼 child —— `/demo📦` 的 slug 是 `demo`，
    # 拼成 `#demo-word` 而不是 `#/demo📦-word`
    root_slug = plan.get("root_slug") or _root_slug(root or "")
    hints = _parse_tag_hints(tag_hints)

    merged, seen = [], {_tag_key(root or "")}
    for h in hints["children"]:
        if _tag_key(h) not in seen:
            seen.add(_tag_key(h))
            merged.append(h)
    for h in hints["bare"]:
        word = h.lstrip("#").strip()
        tag = f"#{root_slug}-{word}" if root_slug else f"#{word}"
        if _tag_key(tag) not in seen:
            seen.add(_tag_key(tag))
            merged.append(tag)
    for tag in plan.get("children") or []:
        if _tag_key(tag) not in seen:
            seen.add(_tag_key(tag))
            merged.append(tag)

    plan["root"] = root
    plan["children"] = merged[:max_children]
    return plan


def infer_tags(title, description="", tag_hints=None):
    """扁平兼容包装：``[root, *children]``（v2.5.0 之前的调用方仍可用）"""
    plan = _merge_tag_plan(
        infer_tags_structured(title, description, tag_hints=tag_hints),
        tag_hints)
    return ([plan["root"]] if plan["root"] else []) + plan["children"]


def _is_url(text):
    """Check if text looks like a URL"""
    return bool(re.match(r'^https?://', text.strip()))


def _is_wechat_url(url):
    """Check if URL is a WeChat article"""
    return "mp.weixin.qq.com" in url.lower()


def _extract_collection_keywords(name):
    """从 collection 名称中提取匹配关键词"""
    keywords = set()
    words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', name)
    for w in words:
        w_lower = w.lower()
        keywords.add(w_lower)
        # 常见变体映射
        if w_lower == "math":
            keywords.add("mathematics")
        elif w_lower == "mathematics":
            keywords.add("math")
        elif w_lower == "settheory":
            keywords.add("set theory")
        elif w_lower == "ai":
            keywords.add("artificial intelligence")
        elif w_lower.startswith("llm"):
            keywords.add("large language model")
        elif w_lower == "cs":
            keywords.add("computer science")
        elif w_lower == "hpc":
            keywords.add("high performance computing")
        if len(w_lower) > 3:
            keywords.add(w_lower[:4])
    return keywords


def _extract_text_keywords(text):
    """从文本中提取关键词，包括合并形式

    Bugfix: 跨学科通用词（如 cycles/kernel/thread/cache/chain/tree/graph/sort/
    search/heap/stack/queue/link/node/path/route）容易与图论/数据结构 collection
    产生误匹配。添加为停用词表，在 text_keywords 层面过滤。
    https://github.com/zzeitt/zot-tool/issues/TODO  （归档时补充 issue 号）
    """
    cross_domain_stopwords = {
        # 通用 CS 词：几乎每个子领域都在用
        "cycle", "cycles", "kernel", "thread", "cache",
        "chain", "tree", "graph", "sort", "sorts", "sorting",
        "search", "heap", "heaps", "stack", "stacks", "queue",
        "link", "links", "linked", "node", "nodes", "edge", "edges",
        "path", "paths", "route", "routes", "routing",
        "split", "merge", "join", "load", "pool", "pools",
        "lock", "locks", "lockfree", "atomic", "sync", "async",
        "pipe", "pipeline", "filter", "map", "reduce",
        "index", "indexing", "scan", "scan", "batch",
        "call", "invoke", "dispatch", "schedule",
        "frame", "buffer", "stream", "chunk", "block",
        "init", "initializer", "alloc", "allocate", "dealloc",
        "handle", "handler", "event", "signal", "interrupt",
        "port", "socket", "host", "client", "server",
        # 通用数学/统计词
        "set", "sets", "function", "model", "models", "learning",
        "train", "test", "data", "feature", "features",
        # 常见动词/形容词（全文搜索时容易误触）
        "get", "set", "put", "add", "remove", "delete", "create", "destroy",
        "new", "old", "first", "last", "next", "prev", "current",
        "high", "low", "fast", "slow", "big", "small", "long", "short",
        "run", "runs", "running", "start", "stop", "end", "ends",
    }
    keywords = set()
    words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text.lower())
    for w in words:
        if w not in cross_domain_stopwords:
            keywords.add(w)
            if len(w) > 4:
                keywords.add(w[:4])
    # 两两相邻词合并（仅非停用词）
    for i in range(len(words) - 1):
        w1, w2 = words[i], words[i+1]
        if w1 not in cross_domain_stopwords and w2 not in cross_domain_stopwords:
            combined = w1 + w2
            keywords.add(combined)
    return keywords


def find_best_collection(title, description):
    """匹配最合适的 collection，无匹配则返回 None

    v1.8.0: 改用 _all_collections() (分页 + 缓存) 替代裸 zot.collections()。
            早返回逻辑保持不变 —— description 为空时依然返回 None,domain
            硬映射已由 archive_url 在调用本函数前先尝试。
    """
    text = (title + " " + description).lower()

    # 如果标题是URL，description也为空，则无法进行有意义的匹配
    if _is_url(title) and not description.strip():
        return None

    text_keywords = _extract_text_keywords(text)
    collections = _all_collections()
    best_match = None
    best_score = 0

    for c in collections:
        name = c['data'].get('name', '')
        key = c['key']
        if key in _get_forbidden_collection_keys():
            continue
        if key == MISC_COLLECTION:
            continue

        coll_keywords = _extract_collection_keywords(name)
        score = len(coll_keywords & text_keywords)

        if score >= 1 and score > best_score:
            best_score = score
            best_match = (key, name)

    return best_match


def create_misc_subcollection(name_hint, url=None):
    """在 Misc 下创建新的子集合，名称格式：Misc--xxx

    v1.8.0: 优先用 url 参数走 _domain_subcoll_name() 硬映射（覆盖 wechat/github/hn/...
    这类已知平台），如果 name_hint 是 URL 也走同样路径。否则退回到 title-slug 算法。
    """
    # 1. URL 路径（显式 url 参数 > name_hint 是 URL）
    target_url = url if url and _is_url(url) else (name_hint if _is_url(name_hint) else None)

    if target_url:
        sub_name = _domain_subcoll_name(target_url) or _fallback_sub_name_from_url(target_url)
    else:
        sub_name = _fallback_sub_name_from_title(name_hint)
    full_name = f"Misc--{sub_name}"

    for c in _all_collections():
        if c['data'].get('name') == full_name:
            print(f"📁 Collection already exists: {full_name}")
            _invalidate_collections_cache()
            return c['key']

    coll_template = {'name': full_name, 'parentCollection': MISC_COLLECTION}
    try:
        resp = zot.create_collections([coll_template])
        if resp.get('successful'):
            new_key = resp['successful']['0']['key']
            print(f"📁 Created new collection: {full_name} ({new_key})")
            _invalidate_collections_cache()
            return new_key
    except Exception as e:
        print(f"⚠️ Failed to create collection: {e}")
    return MISC_COLLECTION


def _fallback_sub_name_from_url(url):
    """URL 路径走完硬映射还没命中时的兜底（取主域名第一段）"""
    domain_match = re.search(r'://([^/]+)', url.lower())
    if domain_match:
        domain = domain_match.group(1)
        if domain.startswith("www."):
            domain = domain[4:]  # www.example.com → example（避免 Misc--www）
        parts = domain.replace(".", " ").split()
        return parts[0] if parts else "web"
    return "web"


def _fallback_sub_name_from_title(name_hint):
    """name_hint 不是 URL 时，从标题文本里取前 2 个有意义的中英文 token"""
    stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "is", "are", "by", "as", "that",
                  "https", "http", "com", "org", "net", "html", "php", "aspx", "weixin", "qq", "mp"}
    words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', name_hint)
    keywords = [w for w in words if len(w) > 2 and w.lower() not in stop_words]
    if keywords:
        return "/".join(keywords[:2]).lower()
    return "uncategorized"


def _detect_binary_url(url):
    """检测 URL 是否为二进制文件（PDF/EPUB 等），返回 (is_binary, content_type, filename_hint)"""
    # 1. URL path 扩展名
    url_lower = url.lower()
    path_hint = None
    # 常见二进制扩展名模式（排除 query string 中的扩展名）
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    bin_exts = {".pdf": "application/pdf", ".epub": "application/epub+zip",
                ".mobi": "application/x-mobipocket-ebook",
                ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".zip": "application/zip"}
    for ext, ct in bin_exts.items():
        if path.endswith(ext):
            path_hint = os.path.basename(path)
            return True, ct, path_hint

    # 2. 已知二进制文件站点的 URL 模式
    # LibGen download pattern
    if "libgen.li/get.php" in url_lower or "booksdl.lc/get.php" in url_lower:
        return True, "application/pdf", None

    # 3. Content-Disposition 重定向目标（需要 HEAD 请求）
    # 简化为按域名判断
    binary_domains = ["libgen.li", "booksdl.lc", "libgen.is", "libgen.rocks",
                      "1lib.sk", "b-ok.cc", "b-ok.org", "bookfi.net", "libgen.fun"]
    for dom in binary_domains:
        if dom in url_lower and ("get.php" in url_lower or "/download" in url_lower):
            return True, "application/pdf", None

    # 4. arXiv: 摘要页（/abs/）与 /pdf/ 均视为 PDF，下载二进制版本
    if "arxiv.org/abs/" in url_lower or "arxiv.org/pdf/" in url_lower:
        m = re.search(r'arxiv\.org/(?:abs|pdf)/([^/?]+)', url_lower)
        arxiv_id = m.group(1) if m else "arxiv"
        return True, "application/pdf", f"{arxiv_id}.pdf"

    return False, None, None


def save_offline_copy(url, parent_item_key, title_hint=None, save_binary=None):
    """保存离线副本（自动识别 HTML 或二进制文件）

    策略（按优先级）：
    1. 若 save_binary 参数显式指定，以其为准
    2. 否则用 _detect_binary_url 检测
    3. HTML：用 monolith 抓取
    4. 二进制：用 archive_binary_url 下载 + 上传
    5. 无 WebDAV：保存到本地目录
    """
    # 检查是 HTML 还是二进制文件
    is_binary, content_type, fname_hint = _detect_binary_url(url)

    # 若 save_binary 显式指定，覆盖检测结果（None=auto, False=force HTML, True=force binary）
    if save_binary is False:
        is_binary = False
    elif save_binary is True:
        is_binary = True

    webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/") + "/"
    webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
    webdav_pass = os.environ.get("ZOTERO_WEBDAV_PASS", "")
    has_webdav = all([webdav_url, webdav_user, webdav_pass])

    # ---- 二进制文件（PDF/EPUB等）----
    if is_binary:
        if not has_webdav:
            print("⚠️  Binary file but WebDAV not configured, skipping offline save")
            return None
        if not content_type:
            content_type = "application/octet-stream"
        print(f"💾 Binary file detected: {content_type}")
        return archive_binary_url(
            url, parent_item_key,
            content_type=content_type,
            filename_hint=fname_hint,
            title_hint=title_hint
        )

    # ---- HTML：用 monolith 抓取 ----
    # 检查 monolith 是否可用
    try:
        cmd = "where" if IS_WINDOWS else "which"
        result = subprocess.run([cmd, "monolith"], capture_output=True, text=True)
        if result.returncode != 0:
            print("⚠️  monolith not installed.")
            return None
    except Exception:
        print("⚠️  monolith not available")
        return None

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r'[^\w\-]', '_', (title_hint or url))[:40]
    filename = f"{timestamp}_{slug}.html"

    print(f"💾 Saving offline copy with monolith...")
    tmp_html = os.path.join(_get_temp_dir(), filename)
    # 字体重页面外挂几百个 .woff2，不加 -F 会在 120-240s 内超时。
    # 内置 Google 系 + 本地 overlay 里的字体重灾域名（见 _domain_wants_no_fonts）。
    monolith_args = ["monolith", "-o", tmp_html]
    if _domain_wants_no_fonts(url):
        monolith_args.append("-F")   # --no-fonts
    monolith_args.append(url)
    try:
        result = subprocess.run(
            monolith_args,
            capture_output=True, text=True, timeout=240  # 字体重页面给更多时间
        )
        if result.returncode != 0:
            # 内置 Google 域名 + -F 仍失败 → 给出明确提示
            if any(gdom in url for gdom in ("googleblog.com", "blog.google")):
                print(f"⚠️  monolith failed (Google 域名): {result.stderr[:200]}")
            else:
                print(f"⚠️  monolith failed: {result.stderr}")
            return None
    except Exception as e:
        print(f"⚠️  monolith error: {e}")
        return None

    if not os.path.exists(tmp_html):
        print("⚠️  Offline file not generated")
        return None

    file_size = os.path.getsize(tmp_html)
    print(f"💾 Offline HTML: {tmp_html} ({file_size} bytes)")
    global _last_offline_file
    _last_offline_file = tmp_html

    # v1.8.1: fix WeChat MP articles whose content is hidden by JS-dependent styles
    try:
        if _fix_wechat_html(tmp_html):
            fixed_size = os.path.getsize(tmp_html)
            print(f"🔧 Post-processed WeChat article: {file_size} → {fixed_size} bytes")
    except Exception as e:
        print(f"⚠️  WeChat HTML post-processing skipped: {e}")

    if has_webdav:
        return _upload_to_webdav(tmp_html, parent_item_key, url, webdav_url, webdav_user, webdav_pass)
    else:
        return _save_local_with_note(tmp_html, parent_item_key, url, filename)


# ---------------------------------------------------------------------------
# v2.1.0 — Image compression
# ---------------------------------------------------------------------------
# 两条路径共用同一压缩核心（_compress_image_bytes 压单图、_compress_images_in_html
# 压 HTML 内嵌图），只有参数/入口不同：
#   - 附件上传（防爆）：_prep_upload_file 单次压到 1920/q80，限制 WebDAV 体积。
#   - note 写入（激进）：_fit_note_under_limit 阶梯压到 ≤ _NOTE_MAX_BYTES。

_IMAGE_MAX_DIM = int(os.environ.get("ZOTERO_IMAGE_MAX_DIM", "1920"))
_IMAGE_QUALITY = int(os.environ.get("ZOTERO_IMAGE_QUALITY", "80"))
_HTML_EXTS = {".html", ".htm"}
_RASTER_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _compress_image_bytes(data, max_dim=None, quality=None, as_jpeg=False):
    """压缩单张图片（bytes → bytes），无收益或失败时返回原始 bytes。

    策略：最长边 > max_dim 时等比例缩放到 max_dim（LANCZOS）；
    JPEG 按 quality 重编码；PNG/WebP 保留格式；动画 GIF/其他格式跳过。
    as_jpeg=True 时无损格式（PNG/WebP 等）转 JPEG（有损）——note 体积预算需要。
    Pillow 为软依赖——未安装或任何异常都静默回退原图，绝不阻断归档。
    max_dim/quality 缺省回落到全局 _IMAGE_MAX_DIM/_IMAGE_QUALITY（附件路径行为不变）。
    """
    if max_dim is None:
        max_dim = _IMAGE_MAX_DIM
    if quality is None:
        quality = _IMAGE_QUALITY
    import io
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        if getattr(img, "n_frames", 1) > 1:
            return data  # 动画图——绝不压成静态
        w, h = img.size
        longest = max(w, h)
        if longest > max_dim:
            scale = max_dim / longest
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            img = img.resize((int(w * scale), int(h * scale)), resample)
        out = io.BytesIO()
        if as_jpeg:
            # note 路径：统一转 JPEG（有损）——照片类 PNG 截图无损压不动
            img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
        elif fmt == "JPEG":
            img.convert("RGB").save(out, "JPEG", quality=quality, optimize=True)
        elif fmt == "PNG":
            img.save(out, "PNG", optimize=True)
        elif fmt == "WEBP":
            img.save(out, "WEBP", quality=quality)
        else:
            return data  # GIF/BMP/其他——跳过
        compressed = out.getvalue()
        return compressed if len(compressed) < len(data) else data
    except Exception:
        return data


def _compress_images_in_html(html, max_dim=None, quality=None, as_jpeg=False):
    """压缩 HTML 字符串内嵌的 base64 图片，返回 (new_html, changed)。纯函数。

    只处理 ``src="data:image/...;base64,..."``：解码 → 压缩 → 重编码回 base64。
    外部 URL 图不处理——附件由 monolith 内联、note 由 org 内联，均已是 base64。
    无净收益或失败时返回原字符串。max_dim/quality/as_jpeg 透传给 _compress_image_bytes。
    """
    import base64 as b64

    if "<img" not in html:
        return html, False

    changed = False

    # org 导出会把 base64 按 76 字符换行(CRLF)，所以字符类里必须含 \s，
    # 解码前再剥掉换行/空白——否则只匹配首行、解出截断的图、压缩成空操作。
    def _replace(m):
        nonlocal changed
        data = b64.b64decode(re.sub(r'\s+', '', m.group(2)))
        compressed = _compress_image_bytes(data, max_dim=max_dim, quality=quality, as_jpeg=as_jpeg)
        if len(compressed) >= len(data):
            return m.group(0)  # 无收益，保留原样
        changed = True
        # 输出格式确定：as_jpeg 恒为 jpeg；否则格式保留（JPEG/PNG/WebP 原样）
        subtype = "jpeg" if as_jpeg else m.group(1).lower().replace("jpg", "jpeg")
        return f'data:image/{subtype};base64,{b64.b64encode(compressed).decode("ascii")}'

    html = re.sub(
        r'data:image/([a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\s]+)',
        _replace, html
    )

    return html, changed


# note 体积预算：Zotero 单条 note 上限约 375KB（超出返回 HTTP 413），留安全余量。
# 只有 note 有这限制（WebDAV HTML 附件没有），所以阶梯压缩只用于 note 路径。
_NOTE_MAX_BYTES = 350_000
_NOTE_IMAGE_STEPS = (
    (1280, 70),
    (1024, 60),
    (896, 60),
    (768, 55),
    (640, 50),
)


def _fit_note_under_limit(html):
    """把 note 内嵌图压到 ≤ _NOTE_MAX_BYTES，并去掉 width/height 属性。

    先无损（保格式）逐级缩放；照片类 PNG 无损压不动，再转 JPEG（有损）；
    仍超限时去掉 <img>（图已在 HTML 附件里），硬保证 note 体积不超上限——
    否则会被 Zotero 以 413 静默拒绝。

    org 的 ``#+attr_html: :width 80%`` 导出为 ``width="80%"``，Zotero 提取内嵌图为
    附件时会丢 ``%`` 变成 ``width="80"``（80px），导致图在 note 里极小；故统一去掉
    width/height，让 Zotero 按自然尺寸渲染（配合 max-width:100% 自适应）。
    """
    compressed = None
    for as_jpeg in (False, True):
        for max_dim, quality in _NOTE_IMAGE_STEPS:
            new_html, _ = _compress_images_in_html(html, max_dim=max_dim, quality=quality, as_jpeg=as_jpeg)
            if len(new_html) <= _NOTE_MAX_BYTES:
                compressed = new_html
                break
        if compressed is not None:
            break
    html = compressed if compressed is not None else re.sub(r'<img[^>]*>', ' [图片] ', html)
    return re.sub(r'<img[^>]*>', lambda m: re.sub(r'\s(?:width|height)="[^"]*"', '', m.group(0)), html)


def _prep_upload_file(src_path):
    """预处理待上传附件，返回 (upload_path, cleanup_path)。

    HTML（内嵌 base64 图）和单独图片文件（.jpg/.png/.webp 等）会被压缩；
    其他二进制（PDF/EPUB/ZIP 等）跳过。cleanup_path 非 None 时表示生成了
    临时文件，调用方需在上传完成后删除。压缩无收益或失败时返回原路径。
    """
    ext = os.path.splitext(src_path)[1].lower()

    if ext in _HTML_EXTS:
        try:
            with open(src_path, "r", encoding="utf-8", errors="replace") as f:
                html = f.read()
            new_html, changed = _compress_images_in_html(html)
        except Exception:
            return src_path, None
        if not changed:
            return src_path, None
        fd, new_path = tempfile.mkstemp(suffix=".html", prefix="zot_compressed_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_html)
        return new_path, new_path

    if ext in _RASTER_EXTS:
        try:
            with open(src_path, "rb") as f:
                data = f.read()
            compressed = _compress_image_bytes(data)
        except Exception:
            return src_path, None
        if len(compressed) >= len(data):
            return src_path, None
        fd, new_path = tempfile.mkstemp(suffix=ext, prefix="zot_compressed_")
        with os.fdopen(fd, "wb") as f:
            f.write(compressed)
        return new_path, new_path

    return src_path, None


def _upload_to_webdav(tmp_file_path, parent_item_key, url, webdav_url, webdav_user, webdav_pass,
                        content_type="text/html", archive_filename=None, existing_key=None):
    """打包为 ZIP，PUT 到 WebDAV，创建或更新 Zotero attachment item

    Args:
        tmp_file_path: 原始文件路径（.html 或 .pdf 等）
        content_type: MIME 类型，默认 text/html
        archive_filename: 存档在 ZIP 内的文件名，默认取 basename
        existing_key: 现有 attachment key，提供时做 in-place 更新而非新建
    """
    import hashlib, zipfile

    # 0. 图片压缩预处理：HTML 内嵌图 / 独立图片文件先压缩，其他二进制跳过。
    #    返回 (upload_path, cleanup_path)：上传用 upload_path；cleanup_path 非 None
    #    时是压缩生成的临时文件，上传完成后需删除。压缩失败自动回退原始文件。
    try:
        upload_path, cleanup_path = _prep_upload_file(tmp_file_path)
    except Exception as e:
        print(f"⚠️  Image compression skipped: {e}")
        upload_path, cleanup_path = tmp_file_path, None

    # 1. 打包为 ZIP（Zotero 附件存储格式）
    zip_path = tmp_file_path + ".zip"
    internal_name = archive_filename or os.path.basename(tmp_file_path)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(upload_path, internal_name)

    # 2. 计算 md5 和 mtime（必须是解压后原始文件的属性）
    # Zotero 客户端下载 ZIP 后会解压，然后验证解压后文件的 md5
    with open(upload_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    mtime = int(os.path.getmtime(upload_path) * 1000)

    # 3. 创建或更新 Zotero attachment item
    # filename 和 contentType 必须是解压后的原始文件属性
    # WebDAV 上存的是 <itemKey>.zip，但 Zotero 客户端解压后按 filename 识别
    if existing_key:
        # In-place 更新：保留 attachment key，只更新文件内容和元数据
        # 使用 raw PATCH 只发送需要变更的字段，避免 update_item() 的全量校验
        # 拒绝 lastRead 等只读字段
        try:
            items = zot.item(existing_key)
            item = items[0] if isinstance(items, list) else items
            version = item['data'].get('version', 0)
            resp = zot.client.patch(
                url=build_url(
                    zot.endpoint,
                    f"/{zot.library_type}/{zot.library_id}/items/{existing_key}",
                ),
                headers={"If-Unmodified-Since-Version": str(version)},
                json={
                    "md5": md5,
                    "mtime": mtime,
                    "filename": internal_name,
                    "title": internal_name,
                    "contentType": content_type,
                },
            )
            resp.raise_for_status()
            attach_key = existing_key
            print(f"📎 Updated attachment item: {attach_key}")
        except Exception as e:
            print(f"⚠️  Failed to update attachment item: {e}")
            return None
    else:
        # 新建 attachment item
        try:
            attach = zot.create_items([{
                "itemType": "attachment",
                "parentItem": parent_item_key,
                "linkMode": "imported_file",
                "title": internal_name,
                "filename": internal_name,
                "contentType": content_type,
                "md5": md5,
                "mtime": mtime
            }])
            attach_key = attach["successful"]["0"]["key"]
            print(f"📎 Created attachment item: {attach_key}")
        except Exception as e:
            print(f"⚠️  Failed to create attachment item: {e}")
            return None

    # 4. PUT ZIP 到 WebDAV
    zip_url = f"{webdav_url}{attach_key}.zip"
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "PUT", "-u", f"{webdav_user}:{webdav_pass}",
             "--data-binary", f"@{zip_path}", zip_url],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            print(f"⚠️  WebDAV ZIP upload failed: {result.stderr}")
            return None
        # 验证
        check = subprocess.run(
            ["curl", "-s", "-I", "-u", f"{webdav_user}:{webdav_pass}", zip_url],
            capture_output=True, text=True, timeout=30
        )
        if "200" not in check.stdout and "201" not in check.stdout:
            print(f"⚠️  WebDAV ZIP verification failed: {check.stdout}")
            return None
        print(f"☁️  Uploaded ZIP to WebDAV: {attach_key}.zip")
    except Exception as e:
        print(f"⚠️  WebDAV upload error: {e}")
        return None

    # 5. PUT .prop 到 WebDAV (Zotero 使用 XML 格式，不是 JSON)
    prop_url = f"{webdav_url}{attach_key}.prop"
    prop_content = f'<properties version="1"><mtime>{mtime}</mtime><hash>{md5}</hash></properties>'
    try:
        prop_res = subprocess.run(
            ["curl", "-s", "-o", os.devnull, "-w", "%{http_code}",
             "-X", "PUT",
             "-H", "Content-Type: text/xml",
             "-u", f"{webdav_user}:{webdav_pass}",
             "--data-binary", prop_content, prop_url],
            capture_output=True, text=True, timeout=30
        )
        prop_code = prop_res.stdout.strip()
        if prop_res.returncode != 0 or not prop_code.startswith("2"):
            print(f"⚠️  WebDAV PROP upload failed (HTTP {prop_code}): {prop_res.stderr}")
            return None
        print(f"☁️  Uploaded PROP to WebDAV: {attach_key}.prop")
    except Exception as e:
        print(f"⚠️  WebDAV PROP upload warning: {e}")
        return None

    # 6. 清理临时 ZIP 文件 + 压缩临时文件（原始文件由调用方负责清理）
    for f in [zip_path, cleanup_path]:
        if f is None:
            continue
        try:
            os.remove(f)
        except OSError:
            pass

    print(f"✅ Offline copy synced to WebDAV. Zotero client will recognize it on next sync.")
    return attach_key


def _save_local_with_note(tmp_html, parent_item_key, url, filename):
    """保存到本地目录，并添加导入说明 note"""
    out_dir = os.environ.get("ZOTERO_OFFLINE_DIR") or _get_offline_dir()
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, filename)

    try:
        os.rename(tmp_html, out_path)
    except OSError:
        import shutil
        shutil.copy2(tmp_html, out_path)
        os.remove(tmp_html)

    file_size = os.path.getsize(out_path)
    print(f"💾 Offline file saved: {out_path} ({file_size} bytes)")

    # 添加导入说明 note
    try:
        note_text = (
            f"<h3>📎 离线网页副本</h3>"
            f"<p>文件名：<code>{filename}</code></p>"
            f"<p>保存路径：<code>{out_dir}</code></p>"
            f"<p>原始 URL：<a href='{url}'>{url}</a></p>"
            f"<hr><p><b>导入方法：</b></p>"
            f"<ol>"
            f"<li>在 Minis 中找到该 HTML 文件</li>"
            f"<li>导出/分享到你的设备</li>"
            f"<li>在 Zotero 客户端中，将文件<strong>拖拽到当前条目</strong>上</li>"
            f"</ol>"
        )
        zot.create_items([{
            'itemType': 'note',
            'parentItem': parent_item_key,
            'note': note_text
        }])
        print(f"📝 Added import instruction note")
    except Exception as e:
        print(f"⚠️  Failed to add note: {e}")

    return out_path


def save_file_attachment(file_path, parent_item_key, content_type, archive_filename=None, title_hint=None):
    """保存任意文件为 Zotero 附件（PDF/EPUB/DOC 等），自动上传 WebDAV

    Args:
        file_path: 本地文件路径
        parent_item_key: 父条目的 key
        content_type: MIME 类型（如 application/pdf、application/epub+zip）
        archive_filename: ZIP 内存档名，默认取 basename
        title_hint: 备用标题（用于 slug 生成）
    """
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return None

    file_size = os.path.getsize(file_path)
    print(f"📄 Attachment: {file_path} ({file_size/1024/1024:.1f} MB)")

    webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/") + "/"
    webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
    webdav_pass = os.environ.get("ZOTERO_WEBDAV_PASS", "")
    has_webdav = all([webdav_url, webdav_user, webdav_pass])

    if not has_webdav:
        print("❌ WebDAV not configured (ZOTERO_WEBDAV_URL/USER/PASS)")
        return None

    fname = archive_filename or os.path.basename(file_path)
    print(f"💾 Uploading {fname} to WebDAV...")

    attach_key = _upload_to_webdav(
        tmp_file_path=file_path,
        parent_item_key=parent_item_key,
        url=None,
        webdav_url=webdav_url,
        webdav_user=webdav_user,
        webdav_pass=webdav_pass,
        content_type=content_type,
        archive_filename=fname
    )

    if attach_key:
        print(f"✅ Attachment saved: {attach_key} ({fname})")
    return attach_key


# ---------------------------------------------------------------------------
# v1.9.0 — Child management: list / detach / reattach
# ---------------------------------------------------------------------------

def list_attachments(parent_key):
    """列出父条目下所有子条目（attachment + note）"""
    try:
        parent = zot.item(parent_key)
        parent_data = parent[0] if isinstance(parent, list) else parent
        parent_data = parent_data.get('data', parent_data)
    except Exception as e:
        print(f"❌ Failed to fetch item {parent_key}: {e}")
        return

    parent_title = parent_data.get('title', 'Unknown')[:60]

    try:
        children = zot.children(parent_key)
    except Exception as e:
        print(f"❌ Failed to fetch children: {e}")
        return

    if not children:
        print(f"\n📎 No children for: {parent_title} ({parent_key})")
        return

    print(f"\n📎 Children of: {parent_title} ({parent_key})\n")
    for i, child in enumerate(children, 1):
        data = child.get('data', {})
        item_type = data.get('itemType', '?')
        title = data.get('title', 'Untitled')
        key = child.get('key', '?')

        # Type badge
        if item_type == 'attachment':
            badge = '📄'
            content_type = data.get('contentType', '?')
            link_mode = data.get('linkMode', '?')
            filename = data.get('filename', '')
            fname_str = f" | {filename}" if filename else ""
            detail = f"{content_type} | linkMode={link_mode}{fname_str}"
        elif item_type == 'note':
            badge = '📝'
            # Show first ~80 chars of note content as preview
            note_text = data.get('note', '')
            # Strip HTML tags for preview
            import re as _re
            preview = _re.sub(r'<[^>]+>', '', note_text).strip()[:80]
            detail = f"\"{preview}...\"" if len(preview) >= 80 else f"\"{preview}\""
        else:
            badge = '❓'
            detail = item_type

        print(f"{i}. {badge} {title}")
        print(f"   🔑 {key} | {detail}\n")


def detach_attachment(child_key):
    """删除指定子条目（attachment 或 note），attachment 会自动清理 WebDAV

    软删除（进回收站）：delete_item 传单个 dict；勿传 list（会批量硬删/purge）。
    """
    try:
        items = zot.item(child_key)
        item = items[0] if isinstance(items, list) else items
        data = item.get('data', {})
        item_type = data.get('itemType', '?')
        title = data.get('title', 'unknown')
        parent_key = data.get('parentItem', '')

        if not parent_key:
            print(f"⚠️  {child_key} has no parent — use 'zot delete {child_key}' instead.")
            return

        zot.delete_item(item)
        print(f"✅ Detached: {title} ({child_key}) [type={item_type}]")

        # Clean up WebDAV for attachment children
        if item_type == 'attachment':
            webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/") + "/"
            webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
            webdav_pass = os.environ.get("ZOTERO_WEBDAV_PASS", "")
            if all([webdav_url, webdav_user, webdav_pass]):
                cleaned = 0
                for ext in ['.zip', '.prop']:
                    try:
                        subprocess.run(
                            ["curl", "-s", "-X", "DELETE", "-u",
                             f"{webdav_user}:{webdav_pass}",
                             f"{webdav_url}{child_key}{ext}"],
                            capture_output=True, text=True, timeout=30
                        )
                        cleaned += 1
                    except Exception:
                        pass
                if cleaned:
                    print(f"☁️  WebDAV files cleaned ({child_key}.zip, .prop)")
    except Exception as e:
        print(f"❌ Failed: {e}")


def reattach_attachment(attach_key, file_path, archive_filename=None):
    """替换指定 attachment 的文件内容，保留 attachment key（in-place 更新）

    Zotero 客户端 sync 时会检测到 mtime/hash 变化，作为版本更新处理，
    而非创建全新的 attachment item。
    """
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return

    # 1. 拿旧 attachment 的 parent key 并验证
    try:
        items = zot.item(attach_key)
        item = items[0] if isinstance(items, list) else items
        data = item.get('data', {})
        if data.get('itemType') != 'attachment':
            print(f"⚠️  {attach_key} is not an attachment (type: {data.get('itemType')})")
            return

        parent_key = data.get('parentItem')
        old_title = data.get('title', 'unknown')

        if not parent_key:
            print(f"❌ Cannot find parent item for attachment {attach_key}")
            return

        print(f"📎 Updating attachment: {old_title} ({attach_key})")
    except Exception as e:
        print(f"❌ Failed to fetch old attachment: {e}")
        return

    # 2. Content type 推断
    ext_map = {
        ".html": "text/html", ".htm": "text/html",
        ".pdf": "application/pdf",
        ".epub": "application/epub+zip",
        ".zip": "application/zip",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    _, ext = os.path.splitext(file_path)
    content_type = ext_map.get(ext.lower(), "application/octet-stream")

    # 3. In-place 更新：保留 attachment key，覆盖 WebDAV 文件
    webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/") + "/"
    webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
    webdav_pass = os.environ.get("ZOTERO_WEBDAV_PASS", "")
    if not all([webdav_url, webdav_user, webdav_pass]):
        print("❌ WebDAV not configured (ZOTERO_WEBDAV_URL/USER/PASS)")
        return

    _upload_to_webdav(
        tmp_file_path=file_path,
        parent_item_key=parent_key,
        url=None,
        webdav_url=webdav_url,
        webdav_user=webdav_user,
        webdav_pass=webdav_pass,
        content_type=content_type,
        archive_filename=archive_filename,
        existing_key=attach_key
    )


# ---------------------------------------------------------------------------
# v1.10.0 — Tag management: tags list / tag add / tag remove / tag set
# ---------------------------------------------------------------------------

def tags_list(item_key):
    """列出某条目的所有 tags"""
    try:
        items = zot.item(item_key)
        item = items[0] if isinstance(items, list) else items
    except Exception as e:
        print(f"❌ Failed to fetch item {item_key}: {e}")
        return

    data = item.get('data', {})
    title = data.get('title', 'Unknown')[:60]
    tags = data.get('tags', [])

    if not tags:
        print(f"\n🏷️  No tags on: {title} ({item_key})")
        return

    print(f"\n🏷️  Tags on: {title} ({item_key})\n")
    for i, t in enumerate(tags, 1):
        tag_name = t.get('tag', '?')
        tag_type = t.get('type', 1)
        type_str = "(auto)" if tag_type == 1 else ""
        print(f"  {i}. {tag_name} {type_str}")


def _tag_type_for(name, vocab_types):
    """写入该 tag 时用的 type。

    库内已有同名 tag → 沿用观测到的 type（避免同一 tag 又多出一份 0/1 双子）；
    否则用 ``_DEFAULT_TAG_TYPE``。
    """
    return vocab_types.get(name, _DEFAULT_TAG_TYPE)


def _tags_update(item_key, tags, mode, tag_type=None):
    """内部：更新 item 的 tags

    Args:
        item_key: 条目 key
        tags: 新 tag 名列表 (如 ['/unread', '#demo-alpha'])
        mode: 'add' | 'remove' | 'set'
        tag_type: 新 tag 的 type，None 表示用 ``_DEFAULT_TAG_TYPE``。

    为什么默认 0（manual）：Zotero 按 (name, type) 把 tag 存成两套。CLI 历史上
    硬编码 ``type: 1``（automatic），而 automatic 桶在标签选择器菜单里对应
    「Show Automatic Tags」开关和**不可撤销**的「Delete Automatic Tags in This
    Library…」。用户手打的 tag 是 manual，于是 CLI 打的每个 tag 都落在另一套
    体系里、可被一键清空。改打 manual 后 CLI 写入与手动策展一致。

    库内已存在的 tag 沿用其观测 type，不再制造新的 0/1 双份。
    """
    try:
        items = zot.item(item_key)
        item = items[0] if isinstance(items, list) else items
    except Exception as e:
        print(f"❌ Failed to fetch item {item_key}: {e}")
        return

    data = item.get('data', {})
    existing = data.get('tags', [])

    try:
        vocab_types = _vocab_tag_types()
    except Exception:                                   # noqa: BLE001
        vocab_types = {}

    def _type_for(name):
        if tag_type is not None:
            return tag_type
        return vocab_types.get(name, _DEFAULT_TAG_TYPE)

    if mode == 'set':
        new_tags = [{'tag': t, 'type': _type_for(t)} for t in tags]
    elif mode == 'add':
        existing_names = {t.get('tag', '') for t in existing}
        new_tags = list(existing)
        added = 0
        for t in tags:
            if t not in existing_names:
                new_tags.append({'tag': t, 'type': _type_for(t)})
                existing_names.add(t)
                added += 1
        if added == 0:
            print(f"⚠️  All tags already present — nothing to add.")
            return
    elif mode == 'remove':
        remove_set = set(tags)
        new_tags = [t for t in existing if t.get('tag', '') not in remove_set]
        removed = len(existing) - len(new_tags)
        if removed == 0:
            print(f"⚠️  None of the specified tags found — nothing to remove.")
            return
    else:
        print(f"❌ Unknown mode: {mode}")
        return

    item['data']['tags'] = new_tags
    try:
        zot.update_item(item)
    except Exception as e:
        print(f"❌ Update failed: {e}")
        return

    title = data.get('title', 'Unknown')[:50]
    tag_names = [t['tag'] for t in new_tags]

    if mode == 'set':
        print(f"✅ Tags set on '{title}': {', '.join(tag_names) if tag_names else '(none)'}")
    elif mode == 'add':
        print(f"✅ Tags added to '{title}': {', '.join(tags)}")
    elif mode == 'remove':
        print(f"✅ Tags removed from '{title}': {', '.join(tags)}")


def tags_add(item_key, *tag_names):
    """添加 tag(s) 到条目（幂等，不重复添加）"""
    if not tag_names:
        print("Usage: zot tag add <item-key> <tag1> [tag2] ...")
        return
    _tags_update(item_key, list(tag_names), 'add')


def tags_remove(item_key, *tag_names):
    """从条目移除指定 tag(s)"""
    if not tag_names:
        print("Usage: zot tag remove <item-key> <tag1> [tag2] ...")
        return
    _tags_update(item_key, list(tag_names), 'remove')


def tags_set(item_key, *tag_names):
    """替换条目的全部 tags（允许清空——不传 tag 则设为空列表）"""
    _tags_update(item_key, list(tag_names), 'set')


# ── v2.5.0: tag vocab / suggest / merge ──────────────────────────────────

def tag_vocab(refresh=False, show_all=False, min_count=3, root=None,
              orphans=False, dupes=False, as_json=False, cache_path=False):
    """打印本库标签词表 —— agent 挑选 tag 的候选池。

    这就是那份「高频 tag 表」，但它是**派生的、永远新鲜的**，不需要手工维护：
    Zotero 支持服务端按频次排序，约 4 个请求即可拿全「用过 ≥min_count 次」的 tag。
    """
    if cache_path:
        print(_vocab_path())
        return

    vocab = load_vocab(force_refresh=refresh)
    roots = vocab.get("roots", [])
    children = vocab.get("children") or {}

    if as_json:
        out = {k: v for k, v in vocab.items() if k != "scores"}
        if min_count > 1:
            out["roots"] = [r for r in roots if r.get("n", 0) >= min_count]
            out["children"] = {
                k: [c for c in v if c.get("n", 0) >= min_count]
                for k, v in children.items()}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    src = vocab.get("source", "?")
    print(f"\n🏷️  Tag vocabulary — source={src}, "
          f"{len(roots)} roots, {vocab.get('count', 0)} tags total")
    import time as _time
    ts = float(vocab.get("generated_ts") or 0)
    if ts:
        age_h = (_time.time() - ts) / 3600.0
        print(f"    cached {age_h:.1f}h ago  ({_vocab_path()})")
    print()

    if orphans:
        orphs = vocab.get("orphans", [])
        print(f"  # tags with no resolvable root ({len(orphs)}) "
              f"— merge candidates:")
        for o in sorted(orphs, key=lambda x: -x.get("n", 0)):
            print(f"    {o['tag']}  ({o.get('n', 0)})")
        if not orphs:
            print("    (none)")
        return

    if dupes:
        groups = {}
        for r in roots:
            for c in children.get(r["slug"], []):
                groups.setdefault((r["slug"], c.get("norm", "")), []).append(c)
        shown = 0
        for (_rslug, norm), lst in sorted(groups.items()):
            if len(lst) < 2:
                continue
            shown += 1
            print(f"  ~{norm}: " + ", ".join(
                f"{c['tag']}({c.get('n', 0)})" for c in lst))
        print(f"\n  {shown} near-duplicate group(s). "
              f"Merge with: zot tag merge <old> <new>")
        return

    for r in roots:
        if root and r["slug"] != root:
            continue
        if not show_all and r.get("n", 0) < min_count:
            continue
        flag = " (status)" if r.get("status") else ""
        print(f"  {r['tag']}  n={r.get('n', 0)}{flag}")
        for c in children.get(r["slug"], []):
            if not show_all and c.get("n", 0) < min_count:
                continue
            print(f"      {c['tag']}  n={c.get('n', 0)}")
    if root and not any(r["slug"] == root for r in roots):
        print(f"  (no root with slug '{root}')")


def tag_suggest(title, description="", as_json=False):
    """不写库的 dry-run：预览 root + children + 新建项 + 待翻译项。

    别名 ``zot tag candidates``。
    """
    if isinstance(description, (list, tuple)):
        description = " ".join(description)
    plan = _merge_tag_plan(infer_tags_structured(title, description))
    final = ([plan["root"]] if plan["root"] else []) + plan["children"]

    if as_json:
        print(json.dumps({**plan, "final": final}, ensure_ascii=False, indent=2))
        return final

    print(f"\n🔎 Tag suggestion for: {title[:70]}")
    print(f"    mode: {plan['mode']}")
    print(f"    root: {plan['root'] or '(none)'}")
    for tag in plan["children"]:
        mark = "♻️" if tag in (plan.get("attached") or []) else "✨"
        print(f"      {mark} {tag}")
    if not plan["children"]:
        print("      (no children)")
    if plan.get("needs"):
        print("    needs:")
        for nd in plan["needs"]:
            print(f"      {nd['concept']} → {nd['want_lang']} "
                  f"(e.g. {nd['want_example']})")
    print(f"    → {', '.join(final) if final else '(nothing)'}")
    return final


def tag_merge(old_tag, new_tag, dry_run=False, limit=0):
    """把 old_tag 全库合并进 new_tag（治理存量发散 tag）。

    Zotero 里同名 tag 会以 type 0/1 两条独立行存在，故必须**显式处理两条**，
    否则合并后仍会残留一条。写完清词表缓存（让 merge 结果立刻对下次归档生效）。
    """
    if not old_tag or not new_tag:
        print("Usage: zot tag merge <old-tag> <new-tag> [--dry-run] [--limit N]")
        return 0
    if _tag_key(old_tag) == _tag_key(new_tag):
        print("⚠️  old and new are the same tag — nothing to do.")
        return 0

    try:
        items = zot.everything(zot.items(tag=old_tag))
    except Exception as e:
        print(f"❌ Failed to query items tagged {old_tag}: {e}")
        return 0

    targets = [i for i in items if is_allowed(i['key'])]
    denied = len(items) - len(targets)
    if limit:
        targets = targets[:limit]
    print(f"\n🏷️  Merge {old_tag} → {new_tag}")
    print(f"    {len(items)} item(s) carry the tag"
          + (f", {denied} skipped (🙊Personal)" if denied else "")
          + (f", {len(targets)} selected (--limit {limit})" if limit else ""))
    if not targets:
        print("ℹ️  Nothing to update.")
        return 0
    if dry_run:
        print(f"    DRY RUN — would update {len(targets)} item(s), nothing written.")
        return len(targets)

    payload, new_key = [], _tag_key(new_tag)
    for it in targets:
        data = it.get('data', {})
        tags = data.get('tags', []) or []
        # 同名 0/1 两条都要清掉；目标 tag 已存在则保留其原有 type
        kept = [t for t in tags if _tag_key(t.get('tag', '')) != _tag_key(old_tag)]
        if not any(_tag_key(t.get('tag', '')) == new_key for t in kept):
            kept.append({'tag': new_tag,
                         'type': _tag_type_for(new_tag, _vocab_tag_types())})
        data['tags'] = kept
        it['data'] = data
        payload.append(it)

    updated = 0
    try:
        if zot.update_items(payload):
            updated = len(payload)
        else:
            raise RuntimeError("update_items returned falsy")
    except Exception as e:                              # noqa: BLE001
        print(f"⚠️  Batch update failed ({e}) — falling back to one-by-one")
        updated = 0
        for it in payload:
            try:
                zot.update_item(it)
                updated += 1
            except Exception as ie:                     # noqa: BLE001
                print(f"❌ {it.get('key', '?')}: {ie}")

    _invalidate_vocab_cache()
    print(f"✅ Merged: {updated} item(s) updated, {old_tag} → {new_tag}")
    return updated


def _download_binary(url, dest_path):
    """下载二进制文件（PDF/EPUB 等），跟随重定向直到最终文件"""
    print(f"⬇️  Downloading: {url}")
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-o", dest_path, "--max-time", "120", url],
            capture_output=True, text=True, timeout=150
        )
        if result.returncode != 0:
            print(f"⚠️  Download failed: {result.stderr}")
            return False
        if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
            print("⚠️  Downloaded file is empty")
            return False
        return True
    except Exception as e:
        print(f"⚠️  Download error: {e}")
        return False


def _ext_for_content_type(ct):
    """根据 MIME 类型推断文件扩展名"""
    mapping = {
        "application/pdf": "pdf",
        "application/epub+zip": "epub",
        "application/zip": "zip",
        "application/msword": "doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.ms-excel": "xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/x-mobipocket-ebook": "mobi",
        "application/octet-stream": "bin",
    }
    return mapping.get(ct, "bin")


def _arxiv_pdf_url(url):
    """将 arXiv 摘要页 URL 重写为 PDF 下载 URL；非 arXiv URL 原样返回"""
    m = re.search(r'arxiv\.org/abs/([^/?]+)', url)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    return url


def archive_binary_url(url, item_key, content_type, filename_hint=None, title_hint=None):
    """下载二进制文件（PDF/EPUB 等）并保存为 Zotero 附件

    Args:
        url: 下载 URL
        item_key: Zotero 条目 key
        content_type: MIME 类型（application/pdf、application/epub+zip 等）
        filename_hint: 下载后的文件名
        title_hint: 用于 slug 生成
    """
    slug = re.sub(r'[^\w\-]', '_', (title_hint or url))[:40]
    default_ext = _ext_for_content_type(content_type)
    fname = f"{filename_hint or slug}.{default_ext}"
    tmp_path = os.path.join(_get_temp_dir(), fname)

    if not _download_binary(_arxiv_pdf_url(url), tmp_path):
        return None

    global _last_offline_file
    _last_offline_file = tmp_path

    return save_file_attachment(
        file_path=tmp_path,
        parent_item_key=item_key,
        content_type=content_type,
        archive_filename=fname,
        title_hint=title_hint
    )


def _fetch_hn_thread_info(url):
    """通过 Algolia API 获取 HN 帖子的标题、作者、热度及热门评论"""
    m = re.search(r'ycombinator\.com/item\?id=(\d+)', url)
    if not m:
        return None
    item_id = m.group(1)
    try:
        result = subprocess.run(
            ["curl", "-s", f"https://hn.algolia.com/api/v1/items/{item_id}"],
            capture_output=True, text=True, timeout=15
        )
        data = json.loads(result.stdout)
        if not data.get("title"):
            return None
        info = {
            "title": data.get("title", ""),
            "url": data.get("url", ""),
            "author": data.get("author", ""),
            "points": data.get("points", 0) or 0,
        }
        # 直接回复帖子的评论，按时间排序（取最新3条）
        children = data.get("children", [])
        direct = [c for c in children if c.get("parent_id") == int(item_id)]
        direct.sort(key=lambda x: x.get("created_at_i", 0), reverse=True)
        top_comments = []
        for c in direct:
            ct = c.get("text", "").strip()
            if ct and len(ct) > 20:
                clean = html.unescape(re.sub(r'<[^>]+>', '', ct))
                top_comments.append({
                    "author": c.get("author", "?"),
                    "text": clean[:300],
                })
                if len(top_comments) >= 3:
                    break
        info["top_comments"] = top_comments
        return info
    except Exception:
        return None

    info["points"] = points
    info["num_comments"] = num_comments
    info["top_comments"] = top_comments
    return info if info.get("title") else None


def _generate_hook(title, description):
    """生成一句吸引人的引言，类似豆瓣电影简介"""
    if not description:
        topic = re.split(r'[|—–\-]', title)[0].strip()
        return f"关于「{topic}」的探讨，内容值得一读。"
    desc_clean = re.sub(r'<[^>]+>', '', description).strip()
    if len(desc_clean) > 150:
        cut = desc_clean[:150]
        for sep in ['。', '. ', '? ', '! ']:
            pos = cut.rfind(sep)
            if pos > 50:
                cut = cut[:pos + 1]
                break
        return cut + "..."
    return desc_clean if desc_clean else "内容值得深入阅读。"


def _md_to_html(text):
    """将 LLM 输出的 markdown 内容转换为 Zotero Note 可用的 HTML"""
    import re
    lines = text.split('\n')
    result = []
    i = 0
    in_list = False

    while i < len(lines):
        line = lines[i]

        # 跳过空行，收集后续
        if not line.strip():
            if in_list:
                result.append('</ol>' if result and '<ol>' in result[-3:] else '</ul>')
                in_list = False
            i += 1
            continue

        # --- 水平线
        if re.match(r'^[-*_]{3,}\s*$', line.strip()):
            result.append('<hr/>')
            i += 1
            continue

        # ### 标题
        m = re.match(r'^#{1,3}\s+(.+)', line)
        if m:
            if in_list:
                result.append('</ol>' if result and '<ol>' in result[-3:] else '</ul>')
                in_list = False
            result.append(f'<h3>{m.group(1)}</h3>')
            i += 1
            continue

        # 列表项
        list_m = re.match(r'^(\d+)\.\s+(.+)', line)
        if list_m:
            if not in_list:
                result.append('<ol>')
                in_list = True
            content = _md_to_html_one_line(list_m.group(2))
            result.append(f'<li>{content}</li>')
            i += 1
            continue

        bullet_m = re.match(r'^[-*]\s+(.+)', line)
        if bullet_m:
            if not in_list:
                result.append('<ul>')
                in_list = True
            content = _md_to_html_one_line(bullet_m.group(2))
            result.append(f'<li>{content}</li>')
            i += 1
            continue

        # 普通段落
        if in_list:
            result.append('</ol>' if result and '<ol>' in result[-3:] else '</ul>')
            in_list = False
        content = _md_to_html_one_line(line)
        result.append(f'<p>{content}</p>')
        i += 1

    if in_list:
        result.append('</ol>' if result and '<ol>' in result[-3:] else '</ul>')

    return '\n'.join(result)


def _md_to_html_one_line(text):
    """处理单行内的 markdown 格式（bold 等）"""
    import re
    # **bold** → <strong>
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # `code` → <code>
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    return text


def _write_pending_summary(title, source_text, item_type, url, parent_key):
    """Write a pending note-summary task for the Claude/agent to process.

    Called when minis-model-use is not available but we're in an agent
    environment.  The agent picks up the task file, reads source_text,
    generates an HTML note, and calls ``zot note set <key>``.
    """
    task = {
        "title": title,
        "source_text": source_text,
        "item_type": item_type,
        "url": url,
        "parent_key": parent_key,
    }
    task_dir = os.path.join(_get_temp_dir(), "zot_pending")
    os.makedirs(task_dir, exist_ok=True)
    task_file = os.path.join(task_dir, f"note_{parent_key}.json")
    with open(task_file, "w", encoding="utf-8") as f:
        json.dump(task, f, ensure_ascii=False, indent=2)
    print(f"[INFO] _llm_summarize: wrote pending note task to {task_file}")
    return task_file


def _write_pending_tags(item_key, plan, title=None, url=None):
    """写「标签待办」文件，交给 agent 异步补全 —— 与 ``note_<key>.json`` 同级。

    两类任务共用 ``$TEMP/zot_pending/`` 目录，agent 已有的「检查 zot_pending/」
    习惯直接覆盖：

    - ``needs_translation``：推断出的 child 还缺中英配对。翻译属 LLM 职责，
      CLI 不能凭空造另一种语言的 slug。agent 翻译后
      ``zot tag add <key> "#<root>-<中文 slug>"``，可顺手把这对登记进
      ``pairs.json``（此后 CLI 就能确定性双语齐打），然后删除本文件。
    - ``vocab_unavailable``：词表完全拿不到，本次只打了 ``/unread``。
      agent 需先 ``zot tag vocab --refresh`` 恢复词表，再补打标签。

    没有待办时**不写文件**（避免每次归档都留下空任务）。
    """
    needs = plan.get("needs") or []
    unavailable = plan.get("mode") == "vocab_unavailable"
    if not needs and not unavailable:
        return None

    if unavailable:
        instructions = [
            "标签词表不可用，本次归档只打了 /unread。",
            "1. 运行 `zot tag vocab --refresh` 重建词表",
            "2. 运行 `zot tag suggest \"<标题>\"` 得到建议的 root + children",
            "3. `zot tag add <item_key> <tag>...` 补打标签",
            "4. 删除本文件",
        ]
    else:
        instructions = [
            "为下列概念补上另一种语言的 slug，逐步完善中文 tag 体系。",
            "1. 逐个翻译 `needs[].concept`，拼成 want_example 所示的 tag 形状",
            "2. `zot tag add <item_key> <tag>...` 写入（tag 必须无空格）",
            "3. 可选：把这对登记进 pairs.json，此后 CLI 会自动双语齐打",
            "   格式 [{\"en\": \"<英文 slug>\", \"zh\": \"<中文 slug>\"}]",
            "4. 删除本文件",
        ]

    task = {
        "item_key": item_key,
        "title": title,
        "url": url,
        "mode": "vocab_unavailable" if unavailable else "needs_translation",
        "attached": plan.get("attached") or [],
        "root": plan.get("root"),
        "children": plan.get("children") or [],
        "needs": needs,
        "pairs_path": _pairs_path(),
        "instructions": instructions,
    }
    try:
        task_dir = os.path.join(_get_temp_dir(), "zot_pending")
        os.makedirs(task_dir, exist_ok=True)
        task_file = os.path.join(task_dir, f"tags_{item_key}.json")
        with open(task_file, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"⚠️  标签待办写入失败: {e}")
        return None
    print(f"🏷️  Tag task queued: {task_file}")
    return task_file


def _llm_summarize(title, description, item_type, url, offline_path=None, parent_key=None):
    """通过 minis-model-use CLI 调用配置的 LLM 生成中文摘要

    调用方式：
      minis-model-use run --model <model> --input-json '{"messages": [{"role": "user", "content": ...}]}'
      → 输出 JSON，含 ok + data.choices[0].message.content 字段

    支持两种模式（v1.8.3 起）：
      - 有 description：5 段式 rich 摘要（基本信息 / 核心结论 / 主要观点 / 元观察 / 延伸方向）
      - 无 description：基于标题+URL 推断的"预期内容指南"，避免 prompt 回声

    若未配置模型或调用失败，返回 None 并降级到规则生成。

    Args:
        offline_path: 离线保存的 HTML 文件路径，若提供则读取正文内容替代 meta description
        parent_key:   Zotero item key（仅 Claude/agent 路径需要，用于异步生成 note）
    """
    import shutil

    # ── Source selection: offline HTML > curl metadata ──
    source_text = ""
    source_label = ""

    # 1) Try offline file first (full article content)
    # 仅 HTML 离线副本可作为正文源；二进制附件（PDF 等）跳过，退回 meta description
    if offline_path and os.path.exists(offline_path):
        try:
            if offline_path.endswith((".html", ".htm")):
                with open(offline_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read()
                import re as _re
                text = _re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<[^>]+>", " ", text)
                text = _re.sub(r"\s+", " ", text).strip()
                source_text = text[:4000]
            else:
                print("[INFO] _llm_summarize: offline file is binary, skipping as source")
            if source_text:
                source_label = "离线正文（前 4000 字）"
                print(f"[INFO] _llm_summarize: read {len(source_text)} chars from offline file")
        except Exception as e:
            print(f"[WARN] _llm_summarize: failed to read offline file: {e}", file=sys.stderr)

    # 2) Fallback: curl metadata
    if not source_text:
        if description and description.strip():
            source_text = description[:2000]
            source_label = "页面 meta description"
        else:
            source_text = ""
            source_label = ""

    has_source = bool(source_text)

    if has_source:
        prompt = f"""你是中文内容摘要助手。请根据以下内容生成结构化中文摘要，用**纯 HTML 格式**输出（不是 markdown！）：

<h3>📋 基本信息</h3>
<p><strong>标题</strong>：{title}</p>
<p><strong>类型</strong>：{item_type}</p>
<p><strong>URL</strong>：{url}</p>

<h3>🎯 核心结论</h3>
<p>（用 2-4 句话高度概括作者的核心主张，避免摘抄原文）</p>

<h3>📝 主要观点</h3>
<ol>
<li>（第一观点，1-2 句话）</li>
<li>（第二观点...）</li>
<li>（第三观点，可选）</li>
</ol>

<h3>💡 值得关注的信息</h3>
<p>（1-3 条文章里没说但读者容易忽略的信号，例如跨篇联系 / 被忽略的细节 / 内部矛盾）</p>

<h3>🔍 延伸方向</h3>
<p>（2-3 个深入阅读方向，帮助读者决定是否要展开研究）</p>

---下面是待摘要的内容（来源：{source_label}）---
**标题**：{title}
**类型**：{item_type}
**URL**：{url}
**内容**：
{source_text}
"""
    else:
        # No source at all — title/URL only
        prompt = f"""你是中文内容摘要助手。原始页面未提供 meta description，请基于标题、类型、URL 推断内容，生成"预期内容指南"。

用**纯 HTML 格式**输出（不是 markdown！）：

<h3>📋 基本信息</h3>
<p><strong>标题</strong>：{title}</p>
<p><strong>类型</strong>：{item_type}</p>
<p><strong>URL</strong>：{url}</p>

<h3>🎯 预期核心议题</h3>
<p>（基于标题推断这篇文章可能讨论的核心议题，2-3 句话）</p>

<h3>💡 平台与定位</h3>
<p>（从 URL 域名推断发布平台，例如 weixin.qq.com = 微信公众号生态；mp.weixin.qq.com/s/ = 单篇文章；github.com/&lt;user&gt;/&lt;repo&gt; = 开源项目主页）</p>

<h3>🔍 阅读建议</h3>
<p>（基于标题+平台类型，判断这篇文章是否值得读、适合谁读，1-2 句话）</p>

---下面是仅有的信息---
**标题**：{title}
**类型**：{item_type}
**URL**：{url}
**描述/摘要**：（无 meta description 可用，请基于标题和 URL 推断）
"""
    # ── Backend dispatch: minis-model-use vs Claude/agent ──
    if shutil.which("minis-model-use"):
        try:
            # 获取可用的模型
            list_res = subprocess.run(
                ["minis-model-use", "list", "--compact"],
                capture_output=True, text=True, timeout=10
            )
            if list_res.returncode != 0:
                print(f"[WARN] _llm_summarize: minis-model-use list failed (rc={list_res.returncode}): {list_res.stderr[:200]}", file=sys.stderr)
                return None
            list_data = json.loads(list_res.stdout)
            models = list_data.get("data", {}).get("models", [])
            if not models:
                print(f"[WARN] _llm_summarize: no models available from minis-model-use list", file=sys.stderr)
                return None
            # 优先选 M2.5（更快），fallback 到第一个
            model_id = next(
                (m.get("model_id") for m in models if "2.5" in m.get("model_id", "")),
                models[0].get("model_id")
            ) or "gpt-4o"

            # 调用 LLM（minis-model-use run 需要 --input <path>）
            input_obj = {
                "messages": [{"role": "user", "content": prompt}]
            }
            tmp = os.path.join(_get_temp_dir(), f"zot_llm_{os.getpid()}.json")
            with open(tmp, "w") as f:
                json.dump(input_obj, f, ensure_ascii=False)
            try:
                result = subprocess.run(
                    ["minis-model-use", "run",
                     "--model", model_id,
                     "--input", tmp],
                    capture_output=True, text=True, timeout=120
                )
            finally:
                try: os.remove(tmp)
                except: pass
            if result.returncode != 0 or not result.stdout.strip():
                print(f"[WARN] _llm_summarize: minis-model-use run failed (rc={result.returncode}); stdout={result.stdout[:100]!r} stderr={result.stderr[:100]!r}", file=sys.stderr)
                return None
            res_data = json.loads(result.stdout)
            if not res_data.get("ok"):
                print(f"[WARN] _llm_summarize: minis-model-use run returned ok=False; response={str(res_data)[:200]}", file=sys.stderr)
                return None
            content = res_data.get("data", {}).get("output_text", "")
            # Strip MiniMax think tags using the closing </think> XML tag as anchor.
            # M2.5 format: <think>\n...\n<\/think>\n\nACTUAL_OUTPUT
            # M2.7 format: <think>...\n<\/think>\n\nACTUAL_OUTPUT
            # We split on the closing tag + newline(s) boundary, keeping everything after it.
            # Using </think> as the delimiter avoids false-positives from bare \n\n in content.
            think_close = "</think>"
            if think_close in content:
                idx = content.rfind(think_close)
                after = content[idx + len(think_close):]
                # skip trailing newlines/whitespace then split on first meaningful \n\n
                after = after.lstrip("\n ")
                if after.startswith("\n"):
                    content = after.lstrip("\n").strip()
                else:
                    content = after.strip()
            else:
                content = content.strip()
            if not content:
                print("[WARN] _llm_summarize: LLM returned empty content after stripping think tags", file=sys.stderr)
                return None
            # If LLM echoed the prompt back, discard
            if "\u4e0b\u9762\u662f\u5f85\u6458\u8981\u7684\u5185\u5bb9" in content:
                print("[WARN] _llm_summarize: LLM echoed prompt back; dropping output", file=sys.stderr)
                return None
            # If output is markdown (not HTML), convert to HTML
            if not content.lstrip().startswith("<") and not content.lstrip().startswith("<h"):
                content = _md_to_html(content)
            return content
        except Exception as e:
            print(f"[WARN] _llm_summarize: unexpected exception {type(e).__name__}: {e}", file=sys.stderr)
            return None
    elif parent_key and url:
        # Claude / agent path: write pending task file so the agent
        # can asynchronously read the content, generate an HTML note,
        # and call `zot note set <key>`.
        _write_pending_summary(title, source_text, item_type, url, parent_key)
        return _LLM_PENDING
    else:
        print("[WARN] _llm_summarize: minis-model-use not found in PATH; skipping LLM summarization", file=sys.stderr)
        return None


def _create_content_note(url, title, item_type, parent_key, offline_path=None):
    """生成内容提纲 Note（中文）

    优先调用 LLM 生成高质量中文摘要；无 LLM 时使用规则降级生成。

    Args:
        offline_path: 离线保存的文件路径（HTML/PDF），LLM 可读取其内容生成更准确的摘要
    """
    url_lower = url.lower()

    # === HN 帖子 ===
    if "ycombinator.com" in url_lower:
        global _cached_hn_info
        if not _cached_hn_info:
            _cached_hn_info = _fetch_hn_thread_info(url)
        hn_info = _cached_hn_info
        if not hn_info:
            return

        # 构建 HN 描述：标题 + 最新评论
        hn_desc = hn_info["title"]
        if hn_info.get("top_comments"):
            comments_text = "\n".join(
                f"- {c['author']}：{c['text'][:150]}"
                for c in hn_info["top_comments"]
            )
            hn_desc += f"\n\n热门评论：\n{comments_text}"

        # 尝试 LLM 生成
        summary = _llm_summarize(title, hn_desc, "HN 热议帖子", url, offline_path=offline_path, parent_key=parent_key)

        if summary is _LLM_PENDING:
            print("📝 HN note generation queued for Claude/agent")
            return
        elif summary:
            note_text = f'<h3>📝 HN 热议速览</h3>\n\n{summary}'
        else:
            # 降级：规则生成
            best_comment = hn_info["top_comments"][0]["text"] if hn_info.get("top_comments") else ""
            note_text = (
                f'<h3>📝 HN 热议速览</h3>'
                f'<p><strong>🔥 {hn_info["points"]} points</strong> · by {hn_info["author"]}</p>'
                f'<p><strong>📰 帖子标题</strong>：{hn_info["title"]}</p>'
                f'<hr/><p><strong>📖 社区热议</strong>：{best_comment[:200]}</p>'
            )
            if hn_info.get("url"):
                note_text += f'<p><strong>🔗 链接</strong>：<a href="{hn_info["url"]}">{hn_info["url"][:70]}</a></p>'
            if hn_info.get("top_comments"):
                note_text += f'<hr/><p><strong>💬 最新评论</strong>：</p><blockquote>'
                note_text += '</blockquote><blockquote>'.join(
                    f'{c["text"]}<br/><em>— {c["author"]}</em>'
                    for c in hn_info["top_comments"]
                )
                note_text += '</blockquote>'

        try:
            zot.create_items([{'itemType': 'note', 'parentItem': parent_key, 'note': note_text}])
            print("📝 Created HN summary note")
        except Exception:
            pass
        return

    # === 通用内容 ===
    desc = getattr(sys.modules[__name__], '_last_fetched_description', '') or ''

    # v1.8.3：无论 desc 是否为空，都尝试调 LLM（之前 desc 为空时直接跳过 LLM
    # 输出"两行 URL"的垃圾 note）。_llm_summarize 内部已支持空 desc 模式。
    summary = _llm_summarize(title, desc, item_type, url, offline_path=offline_path, parent_key=parent_key)

    if summary is _LLM_PENDING:
        print("📝 Note generation queued for Claude/agent")
        return
    elif summary:
        note_text = f'<h3>📝 {title[:60]}...</h3>\n\n{summary}'
    else:
        print(f"[WARN] _create_content_note: _llm_summarize returned None for {url!r}; falling back to rule-based generation (LLM failure — check stderr above for details)", file=sys.stderr)
        # 降级：规则生成
        if desc.strip():
            # 有 description：使用原 hook-based fallback
            hook = _generate_chinese_hook(title, desc[:600])
            type_map = {
                "podcast": ("播客", "本集核心话题"),
                "video": ("视频", "本期核心内容"),
                "preprint": ("论文", "论文核心贡献"),
                "arxiv": ("论文", "论文核心贡献"),
                "book": ("书籍", "本书核心主题"),
                "github": ("项目", "项目亮点"),
                "webpage": ("文章", "文章核心议题"),
            }
            type_label, topic_label = type_map.get(item_type, ("内容", "核心议题"))
            core_topic = re.split(r'[|—–\-]', title)[0].strip()
            note_text = (
                f'<h3>📝 {type_label}速览</h3>'
                f'<p><strong>标题</strong>：{title}</p>'
                f'<hr/>'
                f'<p><strong>📖 中文引言</strong>：{hook}</p>'
                f'<p><strong>{topic_label}</strong>：{core_topic}</p>'
                f'<p><strong>来源</strong>：<a href="{url}">{url[:70]}</a></p>'
                f'<hr/><p><strong>🔍 阅读提示</strong>：先读中文引言判断是否感兴趣，再深入阅读完整内容。</p>'
            )
        else:
            # 无 description 且 LLM 不可用：使用新的 metadata-rich fallback
            # （之前的 v1.8.2 行为是直接 return 不生成 note，导致 note 缺失；
            #  v1.8.3 改为：LLM 也失败时仍生成有元数据的 note + 明确"未生成摘要"标记）
            note_text = _build_minimal_fallback_note(title, url, item_type)

    try:
        zot.create_items([{'itemType': 'note', 'parentItem': parent_key, 'note': note_text}])
        print("📝 Created content summary note")
    except Exception as e:
        print(f"⚠️ Note creation failed: {e}")


def _build_minimal_fallback_note(title, url, item_type):
    """LLM 不可用且无 description 时的 metadata-rich fallback note（v1.8.3+）。

    与 v1.8.2 的"两行 URL 垃圾 note"不同，本函数提供：
      - 类型标签（带 emoji）
      - 标题
      - 发布平台域名（从 URL 提取）
      - URL 路径
      - 完整链接
      - 明确的"未生成摘要"提示 + 修复建议

    让用户在 Zotero 客户端能一眼看出"这是 fallback，不是摘要"。
    """
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
        domain = p.netloc or "(无法解析)"
        path = p.path or "/"
    except Exception:
        domain, path = "(URL 解析失败)", ""

    type_label_map = {
        "podcast": "🎙️ 播客",
        "video": "📺 视频",
        "preprint": "📄 论文",
        "arxiv": "📄 论文",
        "book": "📖 书籍",
        "github": "🛠️ GitHub 项目",
        "webpage": "🌐 网页文章",
    }
    type_label = type_label_map.get(item_type, "📄 内容")

    url_display = url if len(url) <= 70 else url[:70] + "..."

    return (
        f'<h3>📝 自动归档条目（未生成摘要）</h3>'
        f'<p><strong>类型</strong>：{type_label}</p>'
        f'<p><strong>标题</strong>：{title}</p>'
        f'<p><strong>发布平台</strong>：{domain}</p>'
        f'<p><strong>URL 路径</strong>：<code>{path[:120]}</code></p>'
        f'<p><strong>完整链接</strong>：<a href="{url}">{url_display}</a></p>'
        f'<hr/>'
        f'<p style="color:#c00"><em>⚠️ 自动摘要未生成</em></p>'
        f'<ul style="color:#666;font-size:90%">'
        f'<li>原因：原始页面未提供 meta description，且 LLM 摘要不可用（minis-model-use 未配置或调用失败）</li>'
        f'<li>建议：'
        f'<ol style="margin-top:4px">'
        f'<li>打开原文阅读后手动添加摘要；或</li>'
        f'<li>运行 <code>zot addnote &lt;item-key&gt;</code> 重试 LLM 摘要</li>'
        f'</ol></li>'
        f'</ul>'
    )


def _generate_chinese_hook(title, description):
    """根据标题和描述生成一段中文引言（类似豆瓣影评简介风格）

    格式：「关于XXX的探讨/分析/评测。本文/视频聚焦YYY，观点ZZZ，值得关注。」
    """
    # 清洗描述
    desc = re.sub(r'<[^>]+>', '', description).strip()
    if not desc:
        topic = re.split(r'[|—–\-]', title)[0].strip()
        return f"关于「{topic}」的内容，值得深入阅读。"

    topic = re.split(r'[|—–\-]', title)[0].strip()

    # 取描述中有意义的第一段或前 200 字
    lines = [l.strip() for l in desc.split('\n') if l.strip() and len(l.strip()) > 30]
    first_para = lines[0] if lines else desc[:200]

    # 构造中文引言：话题引入 + 内容概括 + 一句话评价
    # 提取关键词（名词/动宾短语）
    words = re.findall(r'[\w]{2,}(?:\s+[\w]{2,})?', first_para[:300])
    key_phrases = [w for w in words if len(w) >= 3][:5]
    key_str = '、'.join(key_phrases[:3])

    # 根据描述内容判断语气和角度
    if any(w in first_para for w in ['how', 'why', 'what', '教程', '指南', '介绍', '讲解']):
        style = "这篇文章深入讲解了"
    elif any(w in first_para for w in ['review', '评测', '测评', '对比', '比较']):
        style = "这篇评测涵盖了"
    elif any(w in first_para for w in ['paper', '研究', '发现', '实验', '发现']):
        style = "这篇研究探讨了"
    elif any(w in first_para for w in ['launch', '发布', '开源', 'release', 'announce']):
        style = "这篇发布介绍了"
    else:
        style = "这篇内容涉及"

    if key_str:
        return f"关于「{topic}」的探讨。{style} {key_str} 等方面，值得关注。"
    else:
        return f"关于「{topic}」的内容，值得深入阅读。"


# 模块级变量用于缓存
_last_fetched_description = ""
_cached_hn_info = None


def archive_url(url, title_hint=None, tag_hints=None, save_offline=True):
    """智能归档 URL 到 Zotero：自动推断 collection 和 tags，默认保存离线副本

    Args:
        url: 目标 URL
        title_hint: 手动指定标题（可选，用于 JS 渲染页面等无法抓取标题的场景）
        tag_hints: 用户建议的标签列表（如 ["#llm", "#visualize"]），会与 infer_tags 结果合并
        save_offline: 是否保存离线 HTML 副本
    """
    global _last_fetched_description
    _last_fetched_description = ""  # reset

    # HN 帖子：先获取真实标题，优化后续匹配
    global _cached_hn_info
    hn_title_override = None
    description = ""
    if "ycombinator.com" in url.lower():
        _cached_hn_info = _fetch_hn_thread_info(url)
        if _cached_hn_info and _cached_hn_info.get("title"):
            hn_title_override = _cached_hn_info["title"]
            description = _cached_hn_info["title"]  # 用 HN 标题作为描述，提升 collection/tag 匹配质量

    # WeChat 文章：标题需要浏览器渲染，curl 抓不到
    if _is_wechat_url(url) and not title_hint:
        print("⚠️  WeChat article detected. Title requires browser rendering.")
        print("⚠️  Suggestion: use 'zot archive <url> \"<title-hint>\"' for better results.")

    print(f"🔍 Fetching metadata for: {url}")
    meta = fetch_url_metadata(url)
    title = title_hint or hn_title_override or meta.get("title", "Untitled")
    description = meta.get("description", "") or description
    _last_fetched_description = description  # 同步给 Note 生成用
    item_type = meta.get("itemType", "webpage")
    if meta.get("error"):
        print(f"⚠️ Metadata fetch warning: {meta['error']}")
    print(f"📄 Title: {title[:80]}")
    print(f"📝 Description: {description[:100] if description else '(no description)'}...")

    # 如果标题是URL且没有任何描述，也没有提供title_hint，则无法进行有效归档
    if _is_url(title) and not description.strip() and not title_hint:
        print("❌ Cannot archive: title is URL and no description available.")
        print("❌ Please provide a title hint: zot archive <url> \"<title>\"")
        return None

    # v2.5.0: 复用优先 —— 从本库既有 tag 词表里选 1 root + ≤7 children。
    # 用户 #tag 提示优先占 children 席位（_merge_tag_plan 单点保证形态）。
    tag_plan = _merge_tag_plan(
        infer_tags_structured(title, description, tag_hints=tag_hints),
        tag_hints)
    final_tags = ([tag_plan["root"]] if tag_plan["root"] else []) + tag_plan["children"]
    if tag_plan["mode"] == "vocab_unavailable":
        print("⚠️  标签词表不可用 —— 本次只打 /unread，不发明新 tag（待办见 zot_pending/）")
    if not final_tags:
        print("🏷️  Tags: (none — no library tag matched)")
    else:
        reused = [t for t in final_tags if t in (tag_plan.get("attached") or [])]
        fresh = [t for t in final_tags if t not in reused]
        print(f"🏷️  Tags: {', '.join(final_tags)}")
        if reused:
            print(f"    ♻️  reused: {', '.join(reused)}")
        if fresh:
            print(f"    ✨ new:    {', '.join(fresh)}")

    # v1.8.0: 域名硬映射优先 (优先于多信号评分,优先于 create_misc_subcollection)
    # 场景: WeChat 文章 description 为空 → find_best_collection 早返回 None
    #       旧代码会 fall through 到 create_misc_subcollection 创建中文长名 coll
    #       修复: 已知平台域名直接命中已有 Misc--<sub> coll
    #
    # 2026-09-20 修正: 当 _domain_subcoll_name() 命中但 Misc--<sub> 还不存在时，
    #   旧代码会 fall through 到 find_best_collection 多信号评分，导致严重误判
    #   （长尾标题撞上 coll 名里的偶然同名词）。
    #   新流程：硬映射命中 → 强制走 create_misc_subcollection 创建 Misc--<sub>，
    #   完全跳过评分。硬映射的可信度高于多信号评分（评分在长尾标题上极易误判）。
    domain_sub = _domain_subcoll_name(url)
    domain_match = _find_existing_domain_collection(url)
    if domain_match:
        coll_key, coll_name = domain_match
        print(f"📁 Domain-mapped collection: {coll_name} (from URL domain)")
    elif domain_sub:
        # 硬映射命中但 coll 还不存在 → 直接创建并跳过评分
        target_name = f"Misc--{domain_sub}"
        coll_key = create_misc_subcollection(target_name, url=url)
        coll_name = target_name
        print(f"📁 Domain-mapped new collection: {coll_name}")
    else:
        matched = find_best_collection(title, description)
        if matched:
            coll_key, coll_name = matched
            print(f"📁 Matched collection: {coll_name}")
        else:
            print("🔨 No matching collection found, creating new Misc--xxx subcollection...")
            coll_key = create_misc_subcollection(title + " " + description, url=url)
            coll_name = "new Misc--xxx"

    # 检查URL是否已存在（避免重复创建）
    existing_items = zot.items(q=url, limit=10)
    allowed = [i for i in existing_items if is_allowed(i['key'])]
    if allowed:
        existing_key = allowed[0]['key']
        existing_title = allowed[0].get('data', {}).get('title', 'Unknown')
        print(f"⚠️  URL already archived as item: {existing_key}")
        print(f"⚠️  Existing title: {existing_title[:60]}")
        print(f"⚠️  Skipping duplicate creation.")
        return existing_key

    item = {
        'itemType': item_type,
        'title': title,
        'url': url,
        'abstractNote': description,
        'tags': ([{'tag': '/unread', 'type': _DEFAULT_TAG_TYPE}]
                 + [{'tag': t, 'type': _DEFAULT_TAG_TYPE} for t in final_tags])
    }
    if item_type == "podcast" and meta.get("seriesTitle"):
        item['seriesTitle'] = meta['seriesTitle']
    if item_type == "preprint":
        item['repository'] = meta.get("repository", "arXiv")

    response = zot.create_items([item])
    if response.get('successful'):
        item_key = response['successful']['0']['key']
        print(f"✅ Created item: {item_key}")

        # 让**下一次**归档就能复用本次新造的 tag（否则复用要等 24h TTL 过期）
        _vocab_note_new_tags(tag_plan.get("new") or [])
        _write_pending_tags(item_key, tag_plan, title=title, url=url)

        items = zot.item(item_key)
        fetched = items[0] if isinstance(items, list) else items
        zot.addto_collection(coll_key, fetched)
        print(f"📁 Archived to collection: {coll_key} ({coll_name})")
        print(f"🏷️  Tagged with: /unread, {', '.join(final_tags)}")

        # 保存离线副本
        if save_offline:
            save_offline_copy(url, item_key, title_hint=title)

        # 生成内容提纲 Note（传递离线文件路径供 LLM 阅读附件内容）
        offline_path = _last_offline_file if save_offline else None
        _create_content_note(url, title, item_type, item_key, offline_path=offline_path)

        # 离线文件已用于 LLM 摘要，清理 temp 文件
        if offline_path and os.path.exists(offline_path):
            try:
                os.remove(offline_path)
            except OSError:
                pass

        return item_key
    else:
        print(f"❌ Failed: {response.get('failed', {})}")
        return None

# ── argparse CLI builder ──────────────────────────────────────────

def _build_parser():
    """Build the argparse parser tree.

    Canonical form:  zot <noun> <verb> [args]
    Aliases (search, archive, add, delete, tags, ...) are resolved by
    _resolve_aliases() before argparse sees them.
    """
    p = argparse.ArgumentParser(
        prog="zot",
        description="Zotero library management CLI  —  zot <noun> <verb> [args]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Conventions:\n"
               "  🚫 🙊Personal collection excluded\n"
               "  📌 New items auto-tagged /unread\n"
               "  🏷️  Tags: /rootEmoji + #root-child, no spaces, 1 root + ≤7\n"
               "  🔤 Sort: item search → relevance; item list/coll → dateAdded▼\n"
               "  📁 Archive: HTML→monolith; PDF/EPUB→direct download",
    )
    subs = p.add_subparsers(dest="command", metavar="<command>")

    # ── item ──────────────────────────────────────────────────
    item = subs.add_parser("item", help="Item management")
    item_s = item.add_subparsers(dest="action", metavar="<action>")

    ia = item_s.add_parser("add", help="Create item")
    ia.add_argument("item_type", help="Item type (webpage, book, ...)")
    ia.add_argument("title")
    ia.add_argument("url")
    ia.add_argument("coll_key", help="Target collection key")
    ia.add_argument("extra", nargs="?", help="Extra JSON fields")

    ir = item_s.add_parser("remove", help="Delete item")
    ir.add_argument("item_key")

    il = item_s.add_parser("list", help="List recent items")
    il.add_argument("limit", nargs="?", type=int, default=10)

    is_ = item_s.add_parser("search", help="Full-text search")
    is_.add_argument("query")
    is_.add_argument("limit", nargs="?", type=int, default=10)

    iar = item_s.add_parser("archive", help="Smart archive URL")
    iar.add_argument("--no-offline", action="store_true", dest="no_offline",
                     help="Skip offline HTML copy")
    iar.add_argument("url")
    iar.add_argument("rest", nargs="*", help="[title-hint] [#tag]...")

    # ── tag ───────────────────────────────────────────────────
    tag = subs.add_parser("tag", help="Tag management and search")
    tag_s = tag.add_subparsers(dest="action", metavar="<action>")

    ta = tag_s.add_parser("add", help="Add tags")
    ta.add_argument("item_key")
    ta.add_argument("tags", nargs="+", help="Tag(s) to add")

    tr = tag_s.add_parser("remove", help="Remove tags")
    tr.add_argument("item_key")
    tr.add_argument("tags", nargs="+", help="Tag(s) to remove")

    ts = tag_s.add_parser("set", help="Replace all tags (none = clear)")
    ts.add_argument("item_key")
    ts.add_argument("tags", nargs="*", default=[], help="New tags")

    tl = tag_s.add_parser("list", help="List tags on an item")
    tl.add_argument("item_key")

    tse = tag_s.add_parser("search", help="Search by tag")
    tse.add_argument("query", help="Tag to search for")
    tse.add_argument("limit", nargs="?", type=int, default=10)

    tv = tag_s.add_parser("vocab", help="Show the library tag vocabulary")
    tv.add_argument("--refresh", action="store_true",
                    help="Force re-fetch from the API")
    tv.add_argument("--all", action="store_true", dest="show_all",
                    help="Include rare tags (below --min-count)")
    tv.add_argument("--min-count", type=int, default=3, dest="min_count",
                    help="Only show tags used at least N times (default 3)")
    tv.add_argument("--root", help="Only show children of this root slug")
    tv.add_argument("--orphans", action="store_true",
                    help="List # tags with no resolvable root (merge worklist)")
    tv.add_argument("--dupes", action="store_true",
                    help="Group children by normalized slug to spot near-duplicates")
    tv.add_argument("--json", action="store_true", dest="as_json")
    tv.add_argument("--cache-path", action="store_true", dest="cache_path",
                    help="Print the vocab cache path and exit")

    tsu = tag_s.add_parser("suggest",
                           help="Dry-run tag inference (writes nothing)")
    tsu.add_argument("title")
    tsu.add_argument("description", nargs="*", default=[])
    tsu.add_argument("--json", action="store_true", dest="as_json")

    tca = tag_s.add_parser("candidates", help="Alias of suggest")
    tca.add_argument("title")
    tca.add_argument("description", nargs="*", default=[])
    tca.add_argument("--json", action="store_true", dest="as_json")

    tm = tag_s.add_parser("merge", help="Merge one tag into another library-wide")
    tm.add_argument("old_tag", help="Tag to eliminate")
    tm.add_argument("new_tag", help="Tag to keep")
    tm.add_argument("--dry-run", action="store_true", dest="dry_run")
    tm.add_argument("--limit", type=int, default=0,
                    help="Max items to update (0 = all)")

    # ── coll ──────────────────────────────────────────────────
    coll = subs.add_parser("coll", help="Collection management")
    coll_s = coll.add_subparsers(dest="action", metavar="<action>")

    coll_s.add_parser("list", help="List all collections")

    cr = coll_s.add_parser("remove", help="Delete a collection")
    cr.add_argument("coll_key")

    cs = coll_s.add_parser("search", help="Find collections by name")
    cs.add_argument("name")

    # ── note ──────────────────────────────────────────────────
    note = subs.add_parser("note", help="Note management")
    note_s = note.add_subparsers(dest="action", metavar="<action>")

    na = note_s.add_parser("add", help="Add LLM summary note (pipe supported)")
    na.add_argument("item_key")
    na.add_argument("content", nargs="?", help="Note content (reads stdin if omitted)")

    ns = note_s.add_parser("set", help="Set note directly, no LLM (pipe supported)")
    ns.add_argument("item_key")
    ns.add_argument("content", nargs="?", help="Note content (reads stdin if omitted)")

    # ── attachment ────────────────────────────────────────────
    att = subs.add_parser("attachment", help="Attachment management")
    att_s = att.add_subparsers(dest="action", metavar="<action>")

    ata = att_s.add_parser("add", help="Upload attachment (needs WebDAV)")
    ata.add_argument("item_key")
    ata.add_argument("file_path")
    ata.add_argument("name", nargs="?")

    atr = att_s.add_parser("remove", help="Delete child item")
    atr.add_argument("child_key")

    atu = att_s.add_parser("update", help="Update attachment in-place")
    atu.add_argument("att_key")
    atu.add_argument("file_path")
    atu.add_argument("name", nargs="?")

    atl = att_s.add_parser("list", help="List child items")
    atl.add_argument("parent_key")

    # ── help ──────────────────────────────────────────────────
    subs.add_parser("help", help="Show this help")

    return p


# ── alias resolution (before argparse) ────────────────────────

_ALIAS_MAP = {
    "search":      ["item", "search"],
    "archive":     ["item", "archive"],
    "add":         ["item", "add"],
    "delete":      ["item", "remove"],
    "list":        ["item", "list"],
    "tags":        ["tag", "list"],
    "collections": ["coll", "list"],
    "collection":  ["coll"],
    "addnote":     ["note", "add"],
    "setnote":     ["note", "set"],
    "attachments": ["attachment", "list"],
    "detach":      ["attachment", "remove"],
    "reattach":    ["attachment", "update"],
}


def _resolve_aliases(argv):
    """Rewrite argv so argparse only sees canonical <noun> <verb> forms.

    Handles:
      - Static aliases (search → item search, tags → tag list, ...)
      - tag <query> → tag search <query> (backward compat)
      - coll <name> → coll search <name> (backward compat)
      - attach <subcmd> → attachment <subcmd> (pass-through subcommand)
      - attach <key> <file> → attachment add <key> <file> (backward compat)
    """
    if len(argv) < 2:
        return argv

    cmd = argv[1]
    tail = argv[2:]

    # Static aliases
    if cmd in _ALIAS_MAP:
        return [argv[0]] + _ALIAS_MAP[cmd] + tail

    # tag <non-subcommand> → tag search <query> [limit]
    # 注意：每加一个 tag 子命令都必须登记到这里，否则 `zot tag vocab` 会被
    # 悄悄改写成 `zot tag search vocab`。
    if cmd == "tag" and tail:
        sub = tail[0]
        if sub not in ("add", "remove", "set", "list", "search",
                       "vocab", "suggest", "candidates", "merge",
                       "-h", "--help"):
            argv = [argv[0], "tag", "search"] + tail
            return argv

    # coll <non-subcommand> → coll search <name>
    if cmd == "coll" and tail:
        sub = tail[0]
        if sub not in ("list", "remove", "search", "-h", "--help"):
            argv = [argv[0], "coll", "search"] + tail
            return argv

    # attach <non-subcommand> → attachment add <key> <file> [name]
    if cmd == "attach":
        if not tail:
            return [argv[0], "attachment"]
        sub = tail[0]
        if sub in ("add", "remove", "update", "list", "-h", "--help"):
            return [argv[0], "attachment"] + tail
        else:
            return [argv[0], "attachment", "add"] + tail

    return argv


def show_help():
    """Print top-level help (for backwards compat — argparse handles --help)."""
    _build_parser().print_help()


# ── attach extension → content-type mapping ────────────────────

_ATTACH_EXT_MAP = {
    ".html": "text/html", ".htm": "text/html",
    ".pdf": "application/pdf",
    ".epub": "application/epub+zip",
    ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# ── dispatch helpers ───────────────────────────────────────────

def _create_note(parent_key, note_html):
    """创建子 note 并检查 API 返回的 failed 字典，返回是否成功。

    pyzotero 的 create_items 只 raise HTTP 错误，不检查响应里的 failed 字段——
    Zotero 对超限 note 会以 HTTP 200 + failed（如 413 "Note ... too long"）静默
    拒绝，若不检查会误报成功（note 实际没写进去）。
    """
    resp = zot.create_items([{'itemType': 'note', 'parentItem': parent_key,
                              'note': note_html}])
    if not isinstance(resp, dict):
        print(f"❌ Note write failed: unexpected response {resp!r}")
        return False
    failed = resp.get("failed")
    if failed:
        msgs = []
        for entry in failed.values():
            if isinstance(entry, dict):
                msgs.append(str(entry.get("message") or entry))
            else:
                msgs.append(str(entry))
        print(f"❌ Note rejected: {'; '.join(msgs)}")
        return False
    return True


def _note_add(item_key, note_content):
    """Shared logic for note add (LLM summarization)."""
    if note_content is None:
        note_content = sys.stdin.read()
    if not note_content.strip():
        print("Error: no note content")
        return
    note_content = note_content.strip()
    summary = _llm_summarize(item_key, note_content, "笔记", "")
    if summary:
        note_to_write = summary
    else:
        print(f"[WARN] LLM summarization failed for item {item_key}; "
              f"falling back to raw content (see stderr for details)", file=sys.stderr)
        note_to_write = note_content
    note_html = f'<h3>📝 内容提纲</h3>\n\n{note_to_write}'
    try:
        note_html = _fit_note_under_limit(note_html)  # note 有体积上限，压到 ≤ 上限
    except Exception:
        pass
    if not _create_note(item_key, note_html):
        sys.exit(1)
    print(f"✅ Added note to item {item_key}")


def _note_set(item_key, note_content):
    """Shared logic for note set (direct write, no LLM)."""
    if note_content is None:
        note_content = sys.stdin.read()
    if not note_content.strip():
        print("Error: no note content")
        return
    note_content = note_content.strip()
    # 诊断：压缩前统计图片，便于排查「图片被删成 [图片] / 没压动」这类问题。
    n_img = len(re.findall(r'<img\b', note_content, re.IGNORECASE))
    n_b64 = note_content.count('data:image')
    size_in = len(note_content)
    try:
        note_content = _fit_note_under_limit(note_content)  # note 有体积上限，压到 ≤ 上限
    except Exception:
        pass  # 压缩异常时回退原内容，绝不阻断 note 写入
    size_out = len(note_content)
    n_img_out = len(re.findall(r'<img\b', note_content, re.IGNORECASE))
    if n_img == 0:
        print(f"📐 note {item_key}: {size_in} 字节，无内嵌图片")
    elif n_img_out == 0:
        print(f"📐 note {item_key}: {size_in}→{size_out} 字节，{n_img} 张图(base64×{n_b64})压缩后仍超上限，已移除→[图片]")
    elif size_out < size_in:
        print(f"📐 note {item_key}: {size_in}→{size_out} 字节，{n_img} 张图已压缩保留")
    else:
        print(f"📐 note {item_key}: {size_in} 字节未变，{n_img} 张图(base64×{n_b64})未压缩")
    if not _create_note(item_key, note_content):
        sys.exit(1)
    print(f"✅ Set note on item {item_key}")


def _attach_add(item_key, file_path, archive_filename=None):
    """Shared logic for attachment add."""
    _, ext = os.path.splitext(file_path)
    content_type = _ATTACH_EXT_MAP.get(ext.lower(), "application/octet-stream")
    save_file_attachment(file_path, item_key, content_type,
                         archive_filename=archive_filename)


def _item_remove(item_key):
    """Shared logic for item remove/delete.

    软删除（进回收站）：delete_item 传单个 dict 走 DELETE /items/{key}；
    若误传 list 会走批量 DELETE（硬删/purge、不进回收站），务必只传单条。
    """
    items = zot.item(item_key)
    item = items[0] if isinstance(items, list) else items
    zot.delete_item(item)
    print(f"✅ Deleted item: {item_key}")


def _coll_remove(coll_key):
    """Shared logic for coll remove."""
    if not _is_collection_empty(coll_key):
        items = list(zot.everything(zot.collection_items(coll_key)))
        print(f"⚠️  Collection has {len(items)} item(s). Remove items first, "
              f"or use --force to override.")
        return
    ok, msg = _delete_collection_raw(coll_key)
    if ok:
        _invalidate_collections_cache()
        print(f"🗑️  Deleted collection: {coll_key}")
    else:
        print(f"❌ Failed: {msg}")


# ── main ───────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. Resolve aliases so argparse only sees canonical forms
    sys.argv = _resolve_aliases(sys.argv)

    # 2. Parse
    parser = _build_parser()
    args = parser.parse_args()

    # 3. Dispatch
    cmd = args.command

    if cmd is None or cmd == "help":
        parser.print_help()
        sys.exit(0)

    if args.action is None:
        # User typed a noun without a verb — show that noun's help
        for _cmd, _sub in [
            ("item", "item"), ("tag", "tag"), ("coll", "coll"),
            ("note", "note"), ("attachment", "attachment"),
        ]:
            if _cmd == cmd:
                parser.parse_args([cmd, "--help"])
                sys.exit(0)
        parser.print_help()
        sys.exit(0)

    try:
        if cmd == "item":
            action = args.action
            if action == "add":
                add_item(args.item_type, args.title, args.url,
                         args.coll_key, args.extra)
            elif action == "remove":
                _item_remove(args.item_key)
            elif action == "list":
                list_items(args.limit)
            elif action == "search":
                if not args.query:
                    print("Usage: zot item search <query> [limit]")
                else:
                    search(args.query, args.limit)
            elif action == "archive":
                # Parse rest into title_hint + #tags
                title_hint = None
                tag_hints = []
                for a in args.rest:
                    if a.startswith("#"):
                        tag_hints.append(a)
                    elif not title_hint:
                        title_hint = a
                archive_url(args.url, title_hint, tag_hints,
                           save_offline=not args.no_offline)

        elif cmd == "tag":
            action = args.action
            if action == "add":
                tags_add(args.item_key, *args.tags)
            elif action == "remove":
                tags_remove(args.item_key, *args.tags)
            elif action == "set":
                tags_set(args.item_key, *args.tags)
            elif action == "list":
                tags_list(args.item_key)
            elif action == "search":
                search_by_tag(args.query, args.limit)
            elif action == "vocab":
                tag_vocab(refresh=args.refresh, show_all=args.show_all,
                          min_count=args.min_count, root=args.root,
                          orphans=args.orphans, dupes=args.dupes,
                          as_json=args.as_json, cache_path=args.cache_path)
            elif action in ("suggest", "candidates"):
                tag_suggest(args.title, args.description, as_json=args.as_json)
            elif action == "merge":
                tag_merge(args.old_tag, args.new_tag,
                          dry_run=args.dry_run, limit=args.limit)

        elif cmd == "coll":
            action = args.action
            if action == "list":
                list_collections()
            elif action == "remove":
                _coll_remove(args.coll_key)
            elif action == "search":
                search_by_collection(args.name)

        elif cmd == "note":
            action = args.action
            if action == "add":
                _note_add(args.item_key, args.content)
            elif action == "set":
                _note_set(args.item_key, args.content)

        elif cmd == "attachment":
            action = args.action
            if action == "add":
                _attach_add(args.item_key, args.file_path, args.name)
            elif action == "remove":
                detach_attachment(args.child_key)
            elif action == "update":
                reattach_attachment(args.att_key, args.file_path,
                                   archive_filename=args.name)
            elif action == "list":
                list_attachments(args.parent_key)

    except Exception as e:
        print(f"❌ Failed: {e}")
        sys.exit(1)