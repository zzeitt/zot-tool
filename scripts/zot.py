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
}

# 5 分钟 TTL 缓存 _all_collections() 的结果，避免每次 archive 都全量拉
_collections_cache = {"data": None, "ts": 0.0}
_COLLECTIONS_CACHE_TTL = 300  # seconds


def _domain_subcoll_name(url):
    """从 URL 提取已知平台的子集合名。未命中返回 None。

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
    for dom, sub in sorted(DOMAIN_TO_SUBCOLL.items(), key=lambda x: -len(x[0])):
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
    """Add a new item to Zotero with automatic /unread tag"""
    item = {
        'itemType': item_type,
        'title': title,
        'url': url,
        'tags': [{'tag': '/unread', 'type': 1}]
    }
    if extra_json:
        import json
        item.update(json.loads(extra_json))
        # Ensure /unread is still present even if extra_json had tags
        tags = item.get('tags', [])
        if not any(t.get('tag') == '/unread' for t in tags):
            tags.append({'tag': '/unread', 'type': 1})
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

def fetch_url_metadata(url):
    """获取 URL 的标题和描述，支持常见平台特殊处理"""
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

    # 通用网页抓取
    try:
        result = subprocess.run(
            ["curl", "-s", "-L", "-A", "Mozilla/5.0", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20
        )
        html = result.stdout
        title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else url
        desc_match = re.search(
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            html, re.IGNORECASE
        )
        if not desc_match:
            desc_match = re.search(
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE
            )
        description = desc_match.group(1).strip()[:500] if desc_match else ""
        item_type = "webpage"
        if re.search(r'podcast|episode|播客', title + description, re.I):
            item_type = "podcast"
        return {"title": title, "description": description, "itemType": item_type}
    except Exception as e:
        return {"title": url, "description": "", "itemType": "webpage", "error": str(e)}


# 缓存已有 tags（模块级，首次调用时加载）
_existing_tags_cache = None

def get_existing_tags():
    """获取 Zotero 库中已有的所有 tags（带缓存）"""
    global _existing_tags_cache
    if _existing_tags_cache is not None:
        return _existing_tags_cache
    try:
        tags_resp = zot.tags(limit=200)
        # API returns a list of tag strings directly (not dicts)
        _existing_tags_cache = tags_resp
        return _existing_tags_cache
    except Exception:
        return []


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


def _fuzzy_match_existing(text_lower, existing_tags):
    """从已有 tags 中模糊匹配（严格匹配，禁止空格 tags）"""
    # 过滤掉带空格的 tags（禁止使用）
    filtered_tags = [t for t in existing_tags if ' ' not in t]

    text_words = re.findall(r'[a-z]+', text_lower)
    text_prefixes = set(w[:4] for w in text_words if len(w) > 2)
    text_full_words = set(text_words)

    matches = []
    for tag in filtered_tags:
        tag_lower = tag.lower()
        tag_words = re.findall(r'[a-z]+', tag_lower)
        if not tag_words:
            continue
        tag_prefixes = set(w[:4] for w in tag_words if len(w) > 2)

        # 严格匹配规则（满足其一）：
        # 1. tag 完整包含在 text 中（短语匹配）
        # 2. 至少 2 个前缀匹配（tag 中的重要词在 text 中出现）
        # 3. 至少 2 个完整词匹配
        prefix_count = len(tag_prefixes & text_prefixes)
        word_count = len(set(tag_words) & text_full_words)
        tag_in_text = tag_lower in text_lower

        if tag_in_text or prefix_count >= 2 or word_count >= 2:
            matches.append(tag)

    return matches


def infer_tags(title, description):
    """推断合适的标签

    策略：
    1. 优先从已有 tags 中模糊匹配（仅返回无空格 tags）
    2. 若无匹配，生成 #tag 格式的精炼 tag（无空格，不超过3个）
    3. 以 emoji 结尾增强辨识度
    """
    text = (title + " " + description).lower()

    # 1. 尝试匹配已有 tags（_fuzzy_match_existing 已过滤空格 tags）
    existing = get_existing_tags()
    matched = _fuzzy_match_existing(text, existing)
    if matched:
        # 去重，最多取3个
        unique = []
        for m in matched:
            if m not in unique:
                unique.append(m)
            if len(unique) >= 3:
                break
        return unique

    # 2. 无匹配时，从内容中提炼核心概念词
    concepts = _extract_concepts(title + " " + description)
    return concepts


def _extract_concepts(text):
    """从文本中提取核心概念，生成 #tag 格式（无空格，不超过3个）"""
    # Unicode 处理：去除所有组合附加符（diacritics）
    # "Gödel" (ö=U+00F6) → "Godel"; 避免 [A-Za-z] 被组合字符拆散
    import unicodedata
    def strip_diacritics(s):
        return ''.join(c for c in unicodedata.normalize('NFD', s)
                       if not unicodedata.combining(c))
    text_stripped = strip_diacritics(text)
    text_lower = text_stripped.lower()

    # 如果文本看起来像URL（而非真实标题），跳过概念规则匹配，直接走 fallback
    is_url_text = _is_url(text)

    # 预设领域关键词到 tag 的映射
    concept_rules = [
        (["ai", "artificial intelligence", "大模型", "gpt", "claude", "llm", "machine learning", "机器学习", "deep learning", "深度学习", "transformer", "neural network"], "#AI-ML🤖"),
        (["programming", "编程", "code", "代码", "developer", "开发", "software", "软件", "coding"], "#编程💻"),
        (["economics", "经济", "finance", "金融", "wealth", "财富", "investment", "投资", "market"], "#经济💰"),
        (["mathematics", "数学", "math", "proof", "theorem", "theorems", "证明", "algebra", "几何", "axiom", "logic"], "#数学🔢"),
        (["philosophy", "哲学", "logic", "logics", "logical", "ethics", "伦理", "metaphysics"], "#哲学🤔"),
        (["podcast", "播客", "episode", "广播", "interview", "访谈"], "#播客🎙️"),
        (["video", "视频", "youtube", "bilibili", "lecture", "讲座"], "#视频📺"),
        (["tutorial", "教程", "guide", "入门", "how to", "cheat sheet", "学习"], "#教程📚"),
        (["tool", "工具", "cli", "plugin", "extension", "library", "framework", "app"], "#工具🛠️"),
        (["paper", "论文", "research", "survey", "arxiv", "academic", "学术", "研究"], "#论文📄"),
        (["book", "书", "reading", "阅读", "literature", "文学", "novel", "小说"], "#书籍📖"),
        (["history", "历史", "historical", "过去", "ancient"], "#历史📜"),
        (["science", "科学", "physics", "物理", "chemistry", "化学", "biology", "生物"], "#科学🔬"),
        (["health", "健康", "medicine", "医学", "medical", "healthcare"], "#健康🏥"),
        (["politics", "政治", "policy", "政策", "government", "社会", "society"], "#政治🌍"),
        (["art", "艺术", "design", "设计", "creative", "绘画", "paint"], "#艺术🎨"),
        (["game", "游戏", "gaming", "娱乐", "entertainment"], "#游戏🎮"),
        (["data", "数据", "statistics", "统计", "analytics", "可视化", "visualization"], "#数据📊"),
        (["music", "音乐", "audio", "sound", "song", "歌曲"], "#音乐🎵"),
        (["image", "图像", "photo", "vision", "graphics", "视觉"], "#图像🖼️"),
        (["security", "安全", "privacy", "隐私", "cryptography", "加密", "密码"], "#安全🔒"),
        (["web", "网络", "internet", "互联网", "cloud"], "#网络🌐"),
        (["business", "商业", "startup", "创业", "company", "marketing", "市场"], "#商业💼"),
        (["housing", "房产", "house", "房子", "买房", "real estate"], "#房产🏠"),
        (["writing", "写作", "content creation", "blog", "博客"], "#写作✍️"),
        (["life", "生活", "lifestyle", "旅行", "travel", "food", "美食"], "#生活🌱"),
    ]

    tags = []

    # URL文本：无法提取有意义的标签，直接返回空列表
    if is_url_text:
        return []

    # 非URL文本：正常进行概念规则匹配（单词边界严格匹配）
    for keywords, tag in concept_rules:
        matched = False
        for kw in keywords:
            # \b 单词边界，中文/英文均可精确匹配，不会误匹配子串
            if re.search(r'\b' + re.escape(kw) + r'\b', text_lower, re.IGNORECASE):
                matched = True
                break
        if matched:
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= 3:
                return tags

    # fallback：提取标题中的核心名词，生成无空格 tag
    if not tags:
        words = re.findall(r'[A-Za-z\u4e00-\u9fff]+', text_stripped)
        stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "is", "are",
                      "this", "that", "it", "by", "as", "from", "at", "up", "out", "about", "into", "over",
                      "what", "which", "who", "when", "where", "why", "how", "all", "some", "any", "each",
                      "tiny", "small", "large", "big", "new", "old", "best", "first", "last", "such",
                      "does", "doesn", "did", "didn", "do", "don", "can", "could", "will", "would",
                      "should", "may", "might", "must", "shall", "mayn", "mightn", "mustn", "shalln",
                      "s", "re", "ve", "ll", "d", "won"}
        candidates = [w for w in words if len(w) > 2 and w.lower() not in stop_words]
        if candidates:
            core = candidates[0].lower()
            emoji = _emoji_for_tag(core)
            tags.append(f"#{core}{emoji}")

    return tags if tags else []


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
    try:
        result = subprocess.run(
            ["monolith", "-o", tmp_html, url],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
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

    # 1. 打包为 ZIP（Zotero 附件存储格式）
    zip_path = tmp_file_path + ".zip"
    internal_name = archive_filename or os.path.basename(tmp_file_path)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(tmp_file_path, internal_name)

    # 2. 计算 md5 和 mtime（必须是解压后原始文件的属性）
    # Zotero 客户端下载 ZIP 后会解压，然后验证解压后文件的 md5
    with open(tmp_file_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    mtime = int(os.path.getmtime(tmp_file_path) * 1000)

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

    # 6. 清理临时 ZIP 文件（原始文件由调用方负责清理）
    for f in [zip_path]:
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
    """删除指定子条目（attachment 或 note），attachment 会自动清理 WebDAV"""
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


def _tags_update(item_key, tags, mode):
    """内部：更新 item 的 tags

    Args:
        item_key: 条目 key
        tags: 新 tag 名列表 (如 ['/unread', '#AI-ML🤖'])
        mode: 'add' | 'remove' | 'set'
    """
    try:
        items = zot.item(item_key)
        item = items[0] if isinstance(items, list) else items
    except Exception as e:
        print(f"❌ Failed to fetch item {item_key}: {e}")
        return

    data = item.get('data', {})
    existing = data.get('tags', [])

    if mode == 'set':
        new_tags = [{'tag': t, 'type': 1} for t in tags]
    elif mode == 'add':
        existing_names = {t.get('tag', '') for t in existing}
        new_tags = list(existing)
        added = 0
        for t in tags:
            if t not in existing_names:
                new_tags.append({'tag': t, 'type': 1})
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

    if not _download_binary(url, tmp_path):
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
    if offline_path and os.path.exists(offline_path):
        try:
            with open(offline_path, "r", encoding="utf-8", errors="replace") as f:
                raw = f.read()
            if offline_path.endswith((".html", ".htm")):
                import re as _re
                text = _re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL | _re.IGNORECASE)
                text = _re.sub(r"<[^>]+>", " ", text)
                text = _re.sub(r"\s+", " ", text).strip()
                source_text = text[:4000]
            else:
                source_text = raw[:2000]
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

    # 合并 tags：用户建议优先（取前3个），不足时用 infer_tags 结果补满3个
    inferred = infer_tags(title, description)
    final_tags = []
    if tag_hints:
        for t in tag_hints:
            tag = t if t.startswith("#") else f"#{t}"
            if tag not in final_tags:
                final_tags.append(tag)
            if len(final_tags) >= 3:
                break
    if len(final_tags) < 3:
        for t in inferred:
            tag = t if t.startswith("#") else f"#{t}"
            if tag not in final_tags:
                final_tags.append(tag)
            if len(final_tags) >= 3:
                break
    print(f"🏷️  Tags: {', '.join(final_tags)}")

    # v1.8.0: 域名硬映射优先 (优先于多信号评分,优先于 create_misc_subcollection)
    # 场景: WeChat 文章 description 为空 → find_best_collection 早返回 None
    #       旧代码会 fall through 到 create_misc_subcollection 创建中文长名 coll
    #       修复: 已知平台域名直接命中已有 Misc--<sub> coll
    domain_match = _find_existing_domain_collection(url)
    if domain_match:
        coll_key, coll_name = domain_match
        print(f"📁 Domain-mapped collection: {coll_name} (from URL domain)")
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
        'tags': [{'tag': '/unread', 'type': 1}] + [{'tag': t, 'type': 1} for t in final_tags]
    }
    if item_type == "podcast" and meta.get("seriesTitle"):
        item['seriesTitle'] = meta['seriesTitle']

    response = zot.create_items([item])
    if response.get('successful'):
        item_key = response['successful']['0']['key']
        print(f"✅ Created item: {item_key}")
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
               "  🏷️  Tags: #keyword🤖, no spaces, max 3\n"
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
    if cmd == "tag" and tail:
        sub = tail[0]
        if sub not in ("add", "remove", "set", "list", "search",
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
    zot.create_items([{'itemType': 'note', 'parentItem': item_key, 'note': note_html}])
    print(f"✅ Added note to item {item_key}")


def _note_set(item_key, note_content):
    """Shared logic for note set (direct write, no LLM)."""
    if note_content is None:
        note_content = sys.stdin.read()
    if not note_content.strip():
        print("Error: no note content")
        return
    zot.create_items([{'itemType': 'note', 'parentItem': item_key,
                        'note': note_content.strip()}])
    print(f"✅ Set note on item {item_key}")


def _attach_add(item_key, file_path, archive_filename=None):
    """Shared logic for attachment add."""
    _, ext = os.path.splitext(file_path)
    content_type = _ATTACH_EXT_MAP.get(ext.lower(), "application/octet-stream")
    save_file_attachment(file_path, item_key, content_type,
                         archive_filename=archive_filename)


def _item_remove(item_key):
    """Shared logic for item remove/delete."""
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