#!/usr/bin/env python3
"""Zotero CLI with collection/tag search and 🙊Personal exclusion"""
import os
import sys
import re
import json
import subprocess
import html
from pyzotero import zotero

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

zot = zotero.Zotero(LIBRARY_ID, "user", API_KEY)

# Cache forbidden items
_forbidden_item_keys = None

def get_forbidden_items():
    """Get all item keys in 🙊Personal collection (recursively)"""
    global _forbidden_item_keys
    if _forbidden_item_keys is not None:
        return _forbidden_item_keys
    
    forbidden = set()
    
    # Get all collections under 🙊Personal (recursive)
    def get_sub_collections(parent_key):
        subs = [parent_key]
        for c in zot.collections():
            if c['data'].get('parentCollection') == parent_key:
                subs.extend(get_sub_collections(c['key']))
        return subs
    
    forbidden_collections = get_sub_collections(FORBIDDEN_COLLECTION)
    
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
    """Build collection key->name mapping"""
    collections = zot.collections()
    return {c['key']: c['data'].get('name', 'Unknown') for c in collections}

def get_item_collections(item_key):
    """Get collection names for an item"""
    collections = zot.collections()
    item_collections = []
    for c in collections:
        coll_key = c['key']
        items = zot.collection_items(coll_key)
        if any(item['key'] == item_key for item in items):
            item_collections.append(c['data'].get('name', 'Unknown'))
    return item_collections

def search(query, limit=10, search_tags=False, search_collections=False):
    """Search items in Zotero library"""
    forbidden = get_forbidden_items()
    
    results = []
    
    if search_collections:
        # Search by collection name
        collections = zot.collections()
        coll_map = get_collection_map()
        for c in collections:
            coll_name = c['data'].get('name', '')
            if query.lower() in coll_name.lower():
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

def search_by_collection(collection_name, limit=10):
    """Search items by collection name"""
    forbidden = get_forbidden_items()
    collections = zot.collections()
    
    results = []
    for c in collections:
        name = c['data'].get('name', '')
        if collection_name.lower() in name.lower():
            items = zot.collection_items(c['key'])
            for item in items:
                if item['key'] not in forbidden:
                    item['_matched_collection'] = name
                    results.append(item)
    
    results = results[:limit]
    print(f"\n📁 Collection search: '{collection_name}'")
    print(f"📚 Found {len(results)} items (excluding 🙊Personal):\n")
    
    for i, item in enumerate(results, 1):
        data = item.get('data', {})
        title = data.get('title', 'No title')
        item_type = data.get('itemType', 'unknown')
        key = item['key']
        coll = item.get('_matched_collection', '')
        print(f"{i}. [{item_type}] {title[:60]}...")
        print(f"   🔑 {key} | 📁 {coll}\n")

def search_by_tag(tag, limit=10):
    """Search items by tag"""
    forbidden = get_forbidden_items()
    all_items = zot.items(limit=100)
    
    results = []
    for item in all_items:
        if item['key'] in forbidden:
            continue
        data = item.get('data', {})
        tags = [t.get('tag', '').lower() for t in data.get('tags', [])]
        if tag.lower() in tags:
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

def search_emacs(limit=20):
    """Quick search for emacs-related items"""
    forbidden = get_forbidden_items()
    all_items = zot.items(limit=100)
    
    results = []
    emacs_keywords = ['emacs', 'elisp', 'org-mode', 'org mode', 'dired', 'spacemacs', 'doom emacs']
    
    for item in all_items:
        if item['key'] in forbidden:
            continue
        data = item.get('data', {})
        title = data.get('title', '').lower()
        
        # Check title
        if any(kw in title for kw in emacs_keywords):
            results.append(item)
        # Check tags
        else:
            tags = [t.get('tag', '').lower() for t in data.get('tags', [])]
            if any(kw in t for t in tags for kw in emacs_keywords):
                results.append(item)
        
        # Check collection
        if item not in results:
            for c in zot.collections():
                if 'emacs' in c['data'].get('name', '').lower():
                    coll_items = zot.collection_items(c['key'])
                    if any(i['key'] == item['key'] for i in coll_items):
                        item['_matched_collection'] = c['data']['name']
                        results.append(item)
                        break
        
        if len(results) >= limit:
            break
    
    print(f"\n🔍 Emacs-related items (excluding 🙊Personal):\n")
    for i, item in enumerate(results, 1):
        data = item.get('data', {})
        title = data.get('title', 'No title')
        item_type = data.get('itemType', 'unknown')
        key = item['key']
        coll = item.get('_matched_collection', '')
        coll_str = f" | 📁 {coll}" if coll else ""
        print(f"{i}. [{item_type}] {title[:65]}...")
        print(f"   🔑 {key}{coll_str}\n")

def list_items(limit=10):
    """List recent items (excluding forbidden)"""
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
    """List all collections (excluding 🙊Personal)"""
    collections = zot.collections()
    forbidden_coll = FORBIDDEN_COLLECTION
    
    print(f"\n📁 Collections (excluding 🙊Personal):\n")
    for c in collections:
        if c['key'] == forbidden_coll:
            continue
        # Check if parent is forbidden
        parent = c['data'].get('parentCollection')
        if parent == forbidden_coll:
            continue
            
        name = c['data'].get('name', 'Unknown')
        key = c['key']
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
    text_lower = text.lower()

    # 预设领域关键词到 tag 的映射
    concept_rules = [
        (["ai", "artificial intelligence", "大模型", "gpt", "claude", "llm", "machine learning", "机器学习", "deep learning", "深度学习", "transformer", "neural network"], "#AI-ML🤖"),
        (["programming", "编程", "code", "代码", "developer", "开发", "software", "软件", "coding"], "#编程💻"),
        (["economics", "经济", "finance", "金融", "wealth", "财富", "investment", "投资", "market"], "#经济💰"),
        (["mathematics", "数学", "math", "proof", "theorem", "证明", "algebra", "几何"], "#数学🔢"),
        (["philosophy", "哲学", "logic", "逻辑", "ethics", "伦理", "metaphysics"], "#哲学🤔"),
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
    for keywords, tag in concept_rules:
        if any(kw in text_lower for kw in keywords):
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= 3:
                return tags

    # fallback：提取标题中的核心名词，生成无空格 tag
    if not tags:
        words = re.findall(r'[A-Za-z\u4e00-\u9fff]+', text)
        stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "is", "are",
                      "this", "that", "it", "by", "as", "from", "at", "up", "out", "about", "into", "over",
                      "what", "which", "who", "when", "where", "why", "how", "all", "some", "any", "each",
                      "tiny", "small", "large", "big", "new", "old", "best", "first", "last", "such"}
        candidates = [w for w in words if len(w) > 2 and w.lower() not in stop_words]
        if candidates:
            # 取最前面的名词
            core = candidates[0].lower()
            emoji = _emoji_for_tag(core)
            tags.append(f"#{core}{emoji}")

    return tags if tags else ["#misc🔗"]


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
    """从文本中提取关键词，包括合并形式"""
    keywords = set()
    words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text.lower())
    for w in words:
        keywords.add(w)
        if len(w) > 4:
            keywords.add(w[:4])
    # 两两相邻词合并
    for i in range(len(words) - 1):
        combined = words[i] + words[i+1]
        keywords.add(combined)
    return keywords


def find_best_collection(title, description):
    """匹配最合适的 collection，无匹配则返回 None"""
    text = (title + " " + description).lower()
    text_keywords = _extract_text_keywords(text)
    collections = zot.collections()
    best_match = None
    best_score = 0

    for c in collections:
        name = c['data'].get('name', '')
        key = c['key']
        if key == FORBIDDEN_COLLECTION:
            continue
        if key == MISC_COLLECTION:
            continue

        coll_keywords = _extract_collection_keywords(name)
        score = len(coll_keywords & text_keywords)

        if score >= 1 and score > best_score:
            best_score = score
            best_match = (key, name)

    return best_match


def create_misc_subcollection(name_hint):
    """在 Misc 下创建新的子集合，名称格式：Misc--xxx"""
    stop_words = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with", "is", "are", "by", "as", "that"}
    words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', name_hint)
    keywords = [w for w in words if len(w) > 2 and w.lower() not in stop_words]
    if keywords:
        sub_name = "/".join(keywords[:2]).lower()
    else:
        sub_name = "uncategorized"
    full_name = f"Misc--{sub_name}"

    existing = zot.collections()
    for c in existing:
        if c['data'].get('name') == full_name:
            print(f"📁 Collection already exists: {full_name}")
            return c['key']

    coll_template = {'name': full_name, 'parentCollection': MISC_COLLECTION}
    try:
        resp = zot.create_collections([coll_template])
        if resp.get('successful'):
            new_key = resp['successful']['0']['key']
            print(f"📁 Created new collection: {full_name} ({new_key})")
            return new_key
    except Exception as e:
        print(f"⚠️ Failed to create collection: {e}")
    return MISC_COLLECTION


def save_offline_copy(url, parent_item_key, title_hint=None):
    """保存离线 HTML 副本

    策略（按优先级）：
    1. 若配置了 WebDAV（ZOTERO_WEBDAV_URL/USER/PASS）：
       自动打包为 ZIP，通过 curl PUT 到 WebDAV，并创建 Zotero attachment item
    2. 否则：保存到本地目录，并添加导入说明 note
    """
    # 检查 monolith 是否可用
    try:
        result = subprocess.run(["which", "monolith"], capture_output=True, text=True)
        if result.returncode != 0:
            print("⚠️  monolith not installed. Run: apk add monolith")
            return None
    except Exception:
        print("⚠️  monolith not available")
        return None

    # 生成文件名
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r'[^\w\-]', '_', (title_hint or url))[:40]
    filename = f"{timestamp}_{slug}.html"

    # 用 monolith 抓取
    print(f"💾 Saving offline copy with monolith...")
    tmp_html = f"/tmp/{filename}"
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

    # 检查是否配置了 WebDAV
    webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/") + "/"
    webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
    webdav_pass = os.environ.get("ZOTERO_WEBDAV_PASS", "")
    has_webdav = all([webdav_url, webdav_user, webdav_pass])

    if has_webdav:
        return _upload_to_webdav(tmp_html, parent_item_key, url, webdav_url, webdav_user, webdav_pass)
    else:
        return _save_local_with_note(tmp_html, parent_item_key, url, filename)


def _upload_to_webdav(tmp_html, parent_item_key, url, webdav_url, webdav_user, webdav_pass):
    """打包为 ZIP，PUT 到 WebDAV，创建 Zotero attachment item"""
    import hashlib, zipfile

    # 1. 打包为 ZIP（Zotero 附件存储格式）
    zip_path = tmp_html.replace(".html", ".zip")
    html_filename = os.path.basename(tmp_html)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(tmp_html, html_filename)

    # 2. 计算 md5 和 mtime（必须是解压后原始文件的属性）
    # Zotero 客户端下载 ZIP 后会解压，然后验证解压后文件的 md5
    with open(tmp_html, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    mtime = int(os.path.getmtime(tmp_html) * 1000)

    # 3. 创建 Zotero attachment item
    # filename 和 contentType 必须是解压后的原始文件属性
    # WebDAV 上存的是 <itemKey>.zip，但 Zotero 客户端解压后按 filename 识别
    html_filename = os.path.basename(tmp_html)
    try:
        attach = zot.create_items([{
            "itemType": "attachment",
            "parentItem": parent_item_key,
            "linkMode": "imported_file",
            "title": html_filename,
            "filename": html_filename,
            "contentType": "text/html",
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
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
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

    # 6. 清理临时文件
    for f in [tmp_html, zip_path]:
        try:
            os.remove(f)
        except OSError:
            pass

    print(f"✅ Offline copy synced to WebDAV. Zotero client will recognize it on next sync.")
    return attach_key


def _save_local_with_note(tmp_html, parent_item_key, url, filename):
    """保存到本地目录，并添加导入说明 note"""
    out_dir = os.environ.get("ZOTERO_OFFLINE_DIR", "/tmp/zotero-offline")
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


def _llm_summarize(title, description, item_type, url):
    """通过 minis-model-use CLI 调用配置的 LLM 生成中文摘要

    调用方式：
      minis-model-use run --model <model> --input-json '{"messages": [{"role": "user", "content": ...}]}'
      → 输出 JSON，含 ok + data.choices[0].message.content 字段

    若未配置模型或调用失败，返回 None 并降级到规则生成。
    """
    prompt = f"""你是一个严格的学术笔记助手。请为以下内容生成一段**中文内容提纲**，用纯 HTML 格式输出（不是 markdown！），结构如下：

<h3>📝 内容提纲</h3>
<p><strong>作者/来源</strong>：（从内容中提取，如无则写"不明"）</p>
<hr/>
<p><strong>核心论点</strong>：（2-4 句话，高度概括作者的核心主张，避免摘抄原文）</p>
<hr/>
<p><strong>主要观点</strong>：</p>
<ol>
<li>（第一观点，1-2 句话）</li>
<li>（第二观点...）</li>
<li>（第三观点...）</li>
</ol>
<hr/>
<p><strong>一句话总结</strong>：（用一句话点明这篇文章/视频/帖子为什么值得关注）</p>

---下面是待摘要的内容---
**标题**：{title}
**类型**：{item_type}
**URL**：{url}
**描述/摘要**：
{description[:2000] if description else "(无详细描述)"}
"""
    try:
        # 获取可用的模型
        list_res = subprocess.run(
            ["minis-model-use", "list", "--compact"],
            capture_output=True, text=True, timeout=10
        )
        if list_res.returncode != 0:
            return None
        list_data = json.loads(list_res.stdout)
        models = list_data.get("data", {}).get("models", [])
        if not models:
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
        tmp = f"/tmp/zot_llm_{os.getpid()}.json"
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
            return None
        res_data = json.loads(result.stdout)
        if not res_data.get("ok"):
            return None
        content = res_data.get("data", {}).get("output_text", "")
        # Strip MiniMax think tags: split on \n\n marker
        # Works for M2.5 (<think>\n...<\/think>) and M2.7 (<think>...) format
        marker = '\n\n'
        if marker in content:
            parts = content.split(marker, 1)
            content = parts[-1].strip()
        else:
            content = content.strip()
        if not content:
            return None
        # If LLM echoed the prompt back, discard
        if "\u4e0b\u9762\u662f\u5f85\u6458\u8981\u7684\u5185\u5bb9" in content:
            return None
        # If output is markdown (not HTML), convert to HTML
        if not content.lstrip().startswith("<") and not content.lstrip().startswith("<h"):
            content = _md_to_html(content)
        return content
    except Exception:
        return None


def _create_content_note(url, title, item_type, parent_key):
    """生成内容提纲 Note（中文）

    优先调用 LLM 生成高质量中文摘要；无 LLM 时使用规则降级生成。
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
        summary = _llm_summarize(title, hn_desc, "HN 热议帖子", url)

        if summary:
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

    # 描述为空时不调用 LLM（容易生成 prompt 回声），直接用规则生成
    if not desc.strip():
        note_text = (
            f'<h3>📝 文章速览</h3>'
            f'<p><strong>标题</strong>：{title}</p>'
            f'<p><strong>来源</strong>：<a href="{url}">{url[:70]}</a></p>'
        )
        try:
            zot.create_items([{'itemType': 'note', 'parentItem': parent_key, 'note': note_text}])
            print("📝 Created content summary note")
        except Exception as e:
            print(f"⚠️ Note creation failed: {e}")
        return

    # 优先 LLM 生成（有描述内容时）
    summary = _llm_summarize(title, desc, item_type, url)

    if summary:
        note_text = f'<h3>📝 {title[:60]}...</h3>\n\n{summary}'
    else:
        # 降级：规则生成中文引言
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

    try:
        zot.create_items([{'itemType': 'note', 'parentItem': parent_key, 'note': note_text}])
        print("📝 Created content summary note")
    except Exception as e:
        print(f"⚠️ Note creation failed: {e}")


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


def archive_url(url, title_hint=None, save_offline=True):
    """智能归档 URL 到 Zotero：自动推断 collection 和 tags，默认保存离线副本"""
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

    print(f"🔍 Fetching metadata for: {url}")
    meta = fetch_url_metadata(url)
    title = title_hint or hn_title_override or meta.get("title", "Untitled")
    description = meta.get("description", "") or description
    _last_fetched_description = description  # 同步给 Note 生成用
    item_type = meta.get("itemType", "webpage")
    if meta.get("error"):
        print(f"⚠️ Metadata fetch warning: {meta['error']}")
    print(f"📄 Title: {title[:80]}")
    print(f"📝 Description: {description[:100]}...")

    tags = infer_tags(title, description)
    print(f"🏷️  Inferred tags: {', '.join(tags)}")

    matched = find_best_collection(title, description)
    if matched:
        coll_key, coll_name = matched
        print(f"📁 Matched collection: {coll_name}")
    else:
        print("🔨 No matching collection found, creating new Misc--xxx subcollection...")
        coll_key = create_misc_subcollection(title + " " + description)
        coll_name = "new Misc--xxx"

    item = {
        'itemType': item_type,
        'title': title,
        'url': url,
        'abstractNote': description,
        'tags': [{'tag': '/unread', 'type': 1}] + [{'tag': t, 'type': 1} for t in tags]
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
        print(f"🏷️  Tagged with: /unread, {', '.join(tags)}")

        # 保存离线副本
        if save_offline:
            save_offline_copy(url, item_key, title_hint=title)

        # 生成内容提纲 Note
        _create_content_note(url, title, item_type, item_key)

        return item_key
    else:
        print(f"❌ Failed: {response.get('failed', {})}")
        return None

def show_help():
    print("""Usage: zot <command> [args]

Commands:
  search <query> [limit]         Search items by title/content
  tag <tag> [limit]              Search items by tag
  coll <collection> [limit]       Search items by collection name
  emacs [limit]                  Search emacs-related items
  list [limit]                   List recent items
  collections                    List all collections
  add <type> <title> <url> <coll> [extra]   Add item with /unread tag
  archive <url> [title-hint]     Smart archive URL with auto collection/tag
  archive --no-offline <url>     Archive without saving offline copy
  addnote <item-key> [content]   Add LLM-generated note to existing item
  delete <item-key>              Delete an item from library
  help                           Show this help

Examples:
  zot search "machine learning" 10
  zot tag "important" 5
  zot coll "Cheat" 20
  zot emacs 20
  zot list 15
  zot collections
  zot add podcast "My Podcast" "https://..." LKRM6B4Y
  zot archive "https://podcasts.apple.com/..."
  zot delete KC5ETPXM

🚫 Note: Items in 🙊Personal collection are always excluded.
📌 Convention: All new items are auto-tagged with /unread.
📌 Archive trigger (env): ZOTERO_ARCHIVE_TRIGGER (default: 【归档到Zotero】)
📌 Offline copy: archive command auto-saves offline HTML via monolith
""")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        show_help()
        sys.exit(0)
    
    cmd = sys.argv[1]
    
    if cmd == "search":
        query = sys.argv[2] if len(sys.argv) > 2 else ""
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        if not query:
            print("Usage: zot search <query> [limit]")
        else:
            search(query, limit)
    
    elif cmd == "tag":
        tag = sys.argv[2] if len(sys.argv) > 2 else ""
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        if not tag:
            print("Usage: zot tag <tag> [limit]")
        else:
            search_by_tag(tag, limit)
    
    elif cmd == "coll" or cmd == "collection":
        coll = sys.argv[2] if len(sys.argv) > 2 else ""
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10
        if not coll:
            print("Usage: zot coll <collection-name> [limit]")
        else:
            search_by_collection(coll, limit)
    
    elif cmd == "emacs":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
        search_emacs(limit)
    
    elif cmd == "list":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        list_items(limit)
    
    elif cmd == "collections":
        list_collections()

    elif cmd == "add":
        item_type = sys.argv[2] if len(sys.argv) > 2 else ""
        title = sys.argv[3] if len(sys.argv) > 3 else ""
        url = sys.argv[4] if len(sys.argv) > 4 else ""
        coll_key = sys.argv[5] if len(sys.argv) > 5 else ""
        extra = sys.argv[6] if len(sys.argv) > 6 else None
        if not all([item_type, title, url, coll_key]):
            print("Usage: zot add <item-type> \"<title>\" <url> <collection-key> [extra-json]")
        else:
            add_item(item_type, title, url, coll_key, extra)

    elif cmd == "addnote":
        # zot addnote <item-key> [note-content]
        # Reads note from stdin if no content arg given
        item_key = sys.argv[2] if len(sys.argv) > 2 else ""
        note_content = sys.argv[3] if len(sys.argv) > 3 else None
        if not item_key:
            print("Usage: zot addnote <item-key> [note-content]")
            print("       echo '内容' | zot addnote <item-key>  (pipe mode)")
        else:
            if note_content is None:
                note_content = sys.stdin.read()
            if not note_content.strip():
                print("Error: no note content")
            else:
                note_content = note_content.strip()
                # 生成中文摘要（优先 LLM）
                summary = _llm_summarize(item_key, note_content, "笔记", "")
                if summary:
                    note_to_write = summary
                else:
                    note_to_write = note_content
                note_html = f'<h3>📝 内容提纲</h3>\n\n{note_to_write}'
                try:
                    zot.create_items([{
                        'itemType': 'note',
                        'parentItem': item_key,
                        'note': note_html
                    }])
                    print(f"✅ Added note to item {item_key}")
                except Exception as e:
                    print(f"❌ Failed: {e}")

    elif cmd == "archive":
        url = sys.argv[2] if len(sys.argv) > 2 else ""
        title_hint = sys.argv[3] if len(sys.argv) > 3 else None
        save_offline = True
        if url == "--no-offline":
            save_offline = False
            url = sys.argv[3] if len(sys.argv) > 3 else ""
            title_hint = sys.argv[4] if len(sys.argv) > 4 else None
        if not url:
            print("Usage: zot archive [--no-offline] <url> [title-hint]")
        else:
            archive_url(url, title_hint, save_offline=save_offline)

    elif cmd == "delete":
        item_key = sys.argv[2] if len(sys.argv) > 2 else ""
        if not item_key:
            print("Usage: zot delete <item-key>")
        else:
            try:
                items = zot.item(item_key)
                item = items[0] if isinstance(items, list) else items
                zot.delete_item(item)
                print(f"✅ Deleted item: {item_key}")
            except Exception as e:
                print(f"❌ Failed: {e}")

    elif cmd == "help":
        show_help()

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)