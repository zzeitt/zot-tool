#!/usr/bin/env python3
"""zot_guard — 归档前的重复条目闸门（zot.py 的 URL 去重形同虚设，这里补上前置守卫）

为什么需要：
  zot.py 的 archive_url() 用 `zot.items(q=url, limit=10)` 查重，而 q= 是**全文搜索**，
  匹配不到 URL 字段 → 同一篇文章重复归档时不会被拦住（2026-10-31 一次跑出 3 份重复条目，
  每份带 16.7MB 离线附件）。本脚本在调用 `zot item archive` 之前先跑一遍，命中就跳过。

用法：
  python3 scripts/zot_guard.py <url> [<url> ...]     # 逐个检查
  python3 scripts/zot_guard.py --json <url>          # 机器可读输出
  python3 scripts/zot_guard.py --scan               # 只扫库并打印索引统计

退出码：
  0 = 没有重复，可以归档
  2 = 命中重复（应静默跳过）
  1 = 出错

判定规则（与 GLOBAL.md 的 URL 去重约定一致）：
  1. 规范化 URL：去掉 utm_* / mc_cid / mc_eid 跟踪参数、去 fragment、去 www.、去 scheme、去尾斜杠、
     其余 query 参数按 key 排序 —— **路径完全一致**才算同一篇文章（不同 path 即使同域名也算新文章）
  2. 精确命中 = 规范化后 host+path 相同
  3. 疑似命中 = 标题分词后共享"稀有词"（文档频率 ≤ 2，claude-mem 的 IDF veto 思路），
     但只是提示，不阻止归档
  4. 🙊Personal 及其子 collection 的条目一律跳过（隐私保护）
"""
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

try:
    from pyzotero import zotero
except ImportError:
    sys.exit("❌ 需要 pyzotero：apk add py3-pyzotero 或 pip install pyzotero")

CACHE_PATH = os.environ.get("ZOTERO_GUARD_CACHE", "/tmp/zot-guard-cache.json")
# 全量扫描实测 4 分钟（4,254 条 / 54 页），300s 的 TTL 意味着几乎每次
# 归档都要重扫一遍。默认放宽到 1 小时，可用 ZOTERO_GUARD_TTL 覆盖。
CACHE_TTL = int(os.environ.get("ZOTERO_GUARD_TTL", "3600"))
TRACKING_EXACT = {"mc_cid", "mc_eid"}


# ── URL 规范化 ──────────────────────────────────────────────────────────────
def normalize_url(url):
    """去跟踪参数 + 去 www./scheme/fragment/尾斜杠，返回 (host, path_with_query, 可读形式)。"""
    if not url:
        return None, None, ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None, None, url
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+$", "", p.path or "")
    if path in ("", "/"):
        path = ""
    kept = []
    for k, v in parse_qsl(p.query, keep_blank_values=True):
        kl = k.lower()
        if kl.startswith("utm_") or kl in TRACKING_EXACT:
            continue
        kept.append((k, v))
    kept.sort()
    query = urlencode(kept)
    canon = urlunsplit(("", host, path, query, ""))
    return host, path + ("?" + query if query else ""), canon


def url_key(url):
    """用于比对的键：host+path(+query)，忽略 scheme 与 www."""
    host, rest, _ = normalize_url(url)
    if host is None:
        return None
    return host + rest


# ── 标题分词（支持中文双字组）────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize_title(title):
    t = (title or "").lower()
    tokens = set(_TOKEN_RE.findall(t))
    cjk = re.findall(r"[一-鿿]+", t)
    for run in cjk:
        if len(run) == 1:
            tokens.add(run)
        for i in range(len(run) - 1):
            tokens.add(run[i:i + 2])
    return {x for x in tokens if x}


# ── 库扫描 ────────────────────────────────────────────────────────────────
def _load_cache():
    """读缓存快照（**不管新鲜度**）。

    新鲜度由调用方判断 —— scan_library 需要拿过期快照里的 ``version``
    走增量，如果这里按 TTL 返回 None，增量路径永远进不去（实测退化成
    每次全量 250s）。
    """
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            blob = json.load(f)
        if isinstance(blob, dict) and isinstance(blob.get("items"), list):
            return blob
    except (OSError, ValueError):
        pass
    return None


def _save_cache(blob):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(blob, f)
    except OSError:
        pass


_COLLECTIONS_CACHE_FILE = os.path.join(
    os.environ.get("ZOTERO_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "zot-cache"),
    "collections.json")
COLLECTIONS_TTL = int(os.environ.get("ZOTERO_COLLECTIONS_TTL", "3600"))


def _collections_cached(zot):
    """复用 zot.py 写的 collection 磁盘缓存（同一份文件、同一套 TTL）。

    冷启动列 660 个 collection 要 21.5s（7 页 × 3s），而每篇文章只关心其中几个 key。
    缓存里**必须**能看到 forbidden 根 —— 看不到就当缓存不可信，重新拉，
    免得用一份"创建隐私 collection 之前"的快照把私密条目扫出来。
    """
    root = os.environ.get("ZOTERO_FORBIDDEN_COLLECTION")
    version = 0
    try:
        with open(_COLLECTIONS_CACHE_FILE, encoding="utf-8") as f:
            blob = json.load(f)
        version = blob.get("version") or 0
        fresh = (time.time() - float(blob.get("ts") or 0)) < COLLECTIONS_TTL
        colls = blob.get("collections") or []
        if fresh and colls and (not root or any(c.get("key") == root for c in colls)):
            return colls
    except (OSError, ValueError):
        pass

    colls = list(zot.everything(zot.collections()))
    try:
        os.makedirs(os.path.dirname(_COLLECTIONS_CACHE_FILE), exist_ok=True)
        tmp = _COLLECTIONS_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": version, "ts": time.time(), "collections": colls},
                      f, ensure_ascii=False)
        os.replace(tmp, _COLLECTIONS_CACHE_FILE)
    except OSError:
        pass
    return colls


def forbidden_collection_keys(zot):
    """🙊Personal 及其所有子孙 collection 的 key 集合。"""
    root = os.environ.get("ZOTERO_FORBIDDEN_COLLECTION")
    if not root:
        return set()
    try:
        colls = _collections_cached(zot)
    except Exception:
        return {root}
    parents = {c["key"]: c["data"].get("parentCollection", False) for c in colls}
    banned = {root}
    changed = True
    while changed:
        changed = False
        for k, par in parents.items():
            if k not in banned and par and par in banned:
                banned.add(k)
                changed = True
    return banned


def _iter_items_since(client, version):
    """按 `since` 显式翻页拉变化的条目。

    不用 ``client.everything()``：它依赖 client 上一次请求残留的 ``self.links``，
    当增量返回 **0 条**时（最常见的情况）会跟着一条过期的 next 链接打转，
    实测直接挂住不返回。显式 start/limit 翻页没有这个隐患。
    """
    start = 0
    while True:
        page = client.items(since=version, itemType="-note", limit=100, start=start)
        for it in page:
            yield it
        if len(page) < 100:
            return
        start += 100


def _slim(it, banned):
    """一条 item → 索引条目；用不上就返回 None。"""
    d = it.get("data", {})
    if d.get("parentItem"):            # 子条目（附件/note）没有独立 URL
        return None
    url = d.get("url")
    if not url:
        return None
    if banned and (set(d.get("collections", [])) & banned):
        return None
    return {
        "key": d.get("key"),
        "title": d.get("title", ""),
        "url": url,
        "date": d.get("date", "") or d.get("dateAdded", "")[:10],
        "itemType": d.get("itemType", ""),
        "collections": d.get("collections", []),
    }


def scan_library(zot):
    """返回 [{key,title,url,date,collections}] —— 只含带 URL 的顶层条目。

    缓存 → **增量** → 全量三级。全量实测 4,254 条 / 43 页 / 约 250s，
    所以只延长 TTL 不够（每次归档都可能是冷扫）；增量用 `since=<library version>`
    只拉变化的条目，实测 1-2 页。
    """
    blob = _load_cache()
    if blob and (time.time() - float(blob.get("ts") or 0)) < CACHE_TTL:
        return blob["items"]

    banned = forbidden_collection_keys(zot)

    if blob and blob.get("version"):
        try:
            changed = list(_iter_items_since(zot, blob["version"]))
            merged = _merge_items(blob.get("items") or [], changed, banned)
            version = zot.last_modified_version()
            _save_cache({"ts": time.time(), "version": version, "items": merged})
            return merged
        except Exception:                      # noqa: BLE001 — 增量失败退回全量
            pass

    items = []
    try:
        for it in zot.everything(zot.top(itemType="-note")):
            slim = _slim(it, banned)
            if slim:
                items.append(slim)
    except Exception as e:
        raise SystemExit(f"❌ 扫描 Zotero 库失败：{e}")

    try:
        version = zot.last_modified_version()
    except Exception:                          # noqa: BLE001
        version = 0
    _save_cache({"ts": time.time(), "version": version, "items": items})
    return items


def _merge_items(cached, changed, banned):
    """增量结果合进索引：deleted 的删掉，其余按 key 覆盖；新条目过同一套过滤。"""
    by_key = {it["key"]: it for it in cached or [] if it.get("key")}
    for it in changed or []:
        d = it.get("data", {})
        key = d.get("key")
        if not key:
            continue
        if d.get("deleted"):
            by_key.pop(key, None)
            continue
        slim = _slim(it, banned)
        if slim:
            by_key[key] = slim
        else:
            by_key.pop(key, None)     # 变成无 URL / 移进黑名单 → 从索引剔除
    return list(by_key.values())


def build_index(items):
    idx = {}
    titles = []
    for it in items:
        k = url_key(it["url"])
        if k:
            idx.setdefault(k, []).append(it)
        titles.append(tokenize_title(it["title"]))
    df = Counter()
    for toks in titles:
        for t in toks:
            df[t] += 1
    return idx, df


# ── 检查 ──────────────────────────────────────────────────────────────────
def slug_to_title(url):
    """从 URL slug 猜一个标题（用于疑似重复检测）：取最后一段路径，去日期尾巴，'-' 换空格。"""
    try:
        seg = urlsplit(url).path.rstrip("/").split("/")[-1]
    except ValueError:
        return ""
    seg = re.sub(r"[-_]?20\d{6,8}$", "", seg)
    seg = re.sub(r"[-_]+", " ", seg).strip()
    return seg


MIN_RARE_LEN = 5  # 稀有词最短长度：过滤 rare / best / first 这类"稀有但没信息量"的词


def check_title_candidates(url, idx, df, all_items, extra_title=None):
    """用标题稀有词找疑似重复（URL 不同但同一篇文章的情况）。

    claude-mem 的 IDF veto 思路：只在一侧出现的低频判别词是强信号。
    但"稀有"不等于"有信息量"（rare / best / top 这类词在长尾标题里到处都是），
    所以额外要求：稀有词长度 ≥ 5，且共享数 ≥ 2，或共享 1 个但整体 Jaccard ≥ 0.35。
    """
    out = []
    title = extra_title or ""
    if not title:
        return out
    toks = tokenize_title(title)
    if not toks:
        return out
    rare = {t for t in toks if df.get(t, 0) <= 2 and len(t) >= MIN_RARE_LEN}
    for it in all_items:
        if url_key(it["url"]) == url_key(url):
            continue
        tt = tokenize_title(it["title"])
        if not tt:
            continue
        shared_rare = rare & tt
        j = len(toks & tt) / max(1, len(toks | tt))
        reason = None
        if len(shared_rare) >= 2:
            reason = "稀有词 " + ",".join(sorted(shared_rare)[:3])
        elif shared_rare and j >= 0.35:
            reason = "稀有词 " + ",".join(sorted(shared_rare)[:3]) + f" (Jaccard {j:.2f})"
        elif j >= 0.6:
            reason = f"标题 Jaccard {j:.2f}"
        if reason:
            out.append({"key": it["key"], "title": it["title"], "url": it["url"],
                        "date": it["date"], "reason": reason, "_n": len(shared_rare), "_j": j})
    out.sort(key=lambda d: (-d["_n"], -d["_j"]))
    for d in out:
        d.pop("_n", None)
        d.pop("_j", None)
    return out


def main(argv):
    args, skip_next = [], False
    for a in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if a == "--title":
            skip_next = True
            continue
        if not a.startswith("--"):
            args.append(a)
    flags = {a for a in argv[1:] if a.startswith("--")}
    as_json = "--json" in flags

    title_arg = None
    for i, a in enumerate(argv):
        if a == "--title" and i + 1 < len(argv):
            title_arg = argv[i + 1]

    lib_id = os.environ.get("ZOTERO_LIBRARY_ID")
    api_key = os.environ.get("ZOTERO_API_KEY")
    if not lib_id or not api_key:
        print("❌ 需要 ZOTERO_LIBRARY_ID / ZOTERO_API_KEY 环境变量")
        return 1

    zot = zotero.Zotero(lib_id, "user", api_key)
    items = scan_library(zot)
    idx, df = build_index(items)

    if "--scan" in flags:
        print(f"📚 库内带 URL 的顶层条目：{len(items)}，唯一规范化 URL：{len(idx)}")
        return 0

    if not args:
        print(__doc__)
        return 0

    results = []
    for url in args:
        res = {"url": url, "normalized": normalize_url(url)[2], "exact": [], "probable": []}
        for it in idx.get(url_key(url), []):
            res["exact"].append({k: it[k] for k in ("key", "title", "url", "date")})
        if not res["exact"]:
            guess = title_arg or slug_to_title(url)
            res["probable"] = check_title_candidates(url, idx, df, items, guess)
        results.append(res)

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for res in results:
            print(f"🔍 {res['url']}")
            print(f"   规范化 → {res['normalized']}")
            if res["exact"]:
                for it in res["exact"]:
                    print(f"   ❌ 重复条目  {it['key']}  {it['title'][:60]}")
                    print(f"      已归档 {it['date']} · {it['url'][:90]}")
                print("   → 跳过归档（已存在）")
            else:
                print("   ✅ 库内无同路径条目，可归档")
                for it in res["probable"][:3]:
                    print(f"   ⚠️ 疑似同题  {it['key']}  {it['title'][:55]}（{it['reason']}）")
                    print(f"      {it['url'][:90]}")

    if any(r["exact"] for r in results):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
