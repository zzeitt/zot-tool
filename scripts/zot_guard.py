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
import time
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

try:
    from pyzotero import zotero
except ImportError:
    sys.exit("❌ 需要 pyzotero：apk add py3-pyzotero 或 pip install pyzotero")

CACHE_PATH = os.environ.get("ZOTERO_GUARD_CACHE", "/tmp/zot-guard-cache.json")
CACHE_TTL = int(os.environ.get("ZOTERO_GUARD_TTL", "300"))
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
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            blob = json.load(f)
        if time.time() - blob.get("ts", 0) < CACHE_TTL:
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


def forbidden_collection_keys(zot):
    """🙊Personal 及其所有子孙 collection 的 key 集合。"""
    root = os.environ.get("ZOTERO_FORBIDDEN_COLLECTION")
    if not root:
        return set()
    try:
        colls = list(zot.everything(zot.collections()))
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


def scan_library(zot):
    """返回 [{key,title,url,date,collections}] —— 只含带 URL 的顶层条目。"""
    cached = _load_cache()
    if cached:
        return cached["items"]

    banned = forbidden_collection_keys(zot)
    items = []
    try:
        for it in zot.everything(zot.top()):
            d = it.get("data", {})
            url = d.get("url")
            if not url:
                continue
            if banned and (set(d.get("collections", [])) & banned):
                continue
            items.append({
                "key": d.get("key"),
                "title": d.get("title", ""),
                "url": url,
                "date": d.get("date", "") or d.get("dateAdded", "")[:10],
                "itemType": d.get("itemType", ""),
                "collections": d.get("collections", []),
            })
    except Exception as e:
        raise SystemExit(f"❌ 扫描 Zotero 库失败：{e}")

    _save_cache({"ts": time.time(), "items": items})
    return items


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
