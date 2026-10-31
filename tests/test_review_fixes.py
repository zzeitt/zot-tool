"""2026-10-31 合并前 review 的必修项回归（I-2 / I-3 / I-4 / I-5 / I-6）。

每个用例对应审查报告里的一条缺陷；注释里写明"修之前是什么行为"，
这样将来谁把行为改回去，测试会指名道姓地失败。
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

from test_unit import _synthetic_vocab


def _raise(msg):
    def _f(*_a, **_k):
        raise RuntimeError(msg)
    return _f


# ---------------------------------------------------------------------------
# I-6: 厂商 / 公开出版物域名必须留在源码里
# ---------------------------------------------------------------------------

class TestVendorDomainsStayInSource:
    """外置到未跟踪 overlay 会让 CI / 换机的机器静默丢失硬映射。"""

    @pytest.mark.parametrize("domain,sub", [
        ("developer.nvidia.com", "nvidia"),
        ("fs.blog", "fs-blog"),
        ("skg.com", "skg"),
    ])
    def test_in_builtin_table(self, zot, domain, sub):
        assert zot.DOMAIN_TO_SUBCOLL.get(domain) == sub, (
            f"{domain} 不该只存在于本地 overlay —— 没有 overlay 的机器会丢映射"
        )
        assert zot._domain_subcoll_name(f"https://{domain}/some/post") == sub

    def test_personal_blogs_may_stay_externalized(self, zot):
        """个人阅读兴趣外置是设计选择，不算缺陷。"""
        for domain in ("lemire.me", "barrd.dev", "gatescans-not-a-domain.com"):
            assert domain not in zot.DOMAIN_TO_SUBCOLL


# ---------------------------------------------------------------------------
# I-2: 失效缓存必须真的生效
# ---------------------------------------------------------------------------

class TestInvalidateVocabCacheActuallyWorks:
    """旧实现只 `os.utime(path, (0,0))`，而新鲜度读的是 JSON 里的 generated_ts
    → 回拨 mtime 是空操作，`tag merge` 的结果最长 24h 不生效。"""

    def _fresh_vocab(self):
        v = _synthetic_vocab()
        v["generated_ts"] = time.time()
        return v

    def test_marks_disk_stale(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        zot._write_vocab_file(self._fresh_vocab())
        assert zot._read_vocab_file()["generated_ts"] > 0

        zot._invalidate_vocab_cache()

        assert zot._read_vocab_file()["generated_ts"] == 0, (
            "失效后 generated_ts 必须置 0，否则下一次加载仍判为新鲜"
        )

    def test_next_load_refetches_then_falls_back(self, zot, tmp_path, monkeypatch):
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        zot._write_vocab_file(self._fresh_vocab())
        zot._invalidate_vocab_cache()

        calls = {"n": 0}

        def counting_fetch():
            calls["n"] += 1
            raise RuntimeError("network down")

        monkeypatch.setattr(zot, "_fetch_all_tags_raw", counting_fetch)
        vocab = zot.load_vocab(force_refresh=False)

        assert calls["n"] == 1, "失效后应当重新拉取"
        assert vocab["source"] == "stale", "拉取失败应回退到磁盘陈旧词表"
        assert any(r["tag"] == "/demo📦" for r in vocab["roots"])


# ---------------------------------------------------------------------------
# I-4: 降级路径不能把"有磁盘词表"变成"无词表"
# ---------------------------------------------------------------------------

class TestDegradationPaths:
    def test_force_refresh_still_falls_back_to_disk(self, zot, tmp_path, monkeypatch):
        """`zot tag vocab --refresh` 网络失败时，磁盘词表仍是兜底。

        修之前：force_refresh 直接把 disk 置 None → 打印"无本地词表可用"，
        与文档反复强调的"陈旧词表严格优于无词表"矛盾。
        """
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path))
        v = _synthetic_vocab()
        v["generated_ts"] = time.time()
        zot._write_vocab_file(v)
        monkeypatch.setattr(zot, "_fetch_all_tags_raw", _raise("network down"))

        vocab = zot.load_vocab(force_refresh=True)

        assert vocab["source"] == "stale"
        assert any(r["tag"] == "/demo📦" for r in vocab["roots"])

    def test_empty_vocab_does_not_invent_tags(self, zot):
        """拉到了但词表是空的 → 与"拉不到"等价，不许发明 tag。

        修之前：source 是 "api"，守卫只拦 "vocab_unavailable" → 照常造 /study🔗。
        """
        empty = zot._empty_vocab("api")
        plan = zot.infer_tags_structured("Understanding the Smallest Brain", vocab=empty)
        assert plan["root"] is None
        assert plan["mode"] == "vocab_unavailable"
        assert plan["children"] == []


# ---------------------------------------------------------------------------
# I-3: 兜底 root 不许是体裁词 / 泛化词
# ---------------------------------------------------------------------------

class TestFallbackRootQuality:
    @pytest.mark.parametrize("title,banned_slug", [
        ("Notes on zzqqxx widgetry", "notes"),
        ("Understanding the Smallest Brain You Can Build", "understanding"),
        ("Why Everyone Should Read About Retrieval", "everyone"),
    ])
    def test_no_generic_root(self, zot, title, banned_slug):
        plan = zot.infer_tags_structured(title, vocab=_synthetic_vocab())
        got = (plan.get("root") or "")
        assert banned_slug not in got.casefold(), f"{title!r} 造出了垃圾 root {got}"

    def test_prefers_longest_informative_word(self, zot):
        """取信息量最高的词，而不是标题里第一个非停用词。"""
        plan = zot.infer_tags_structured("tiny tokenised xylophonist", vocab=_synthetic_vocab())
        assert plan["root"] and "xylophonist" in plan["root"], (
            f"应取最长词 xylophonist，实得 {plan['root']}"
        )


# ---------------------------------------------------------------------------
# I-5: 用户 #tag 提示产生的新 child 必须出现在 new 里
# ---------------------------------------------------------------------------

class TestNewTagsReported:
    def test_user_hint_child_reported_as_new(self, zot):
        """`tag suggest --json` 的 new 字段是 agent 决定写什么的依据。

        修之前：new 在 `_merge_tag_plan` **之前**算好，提示产生的 child 永远漏报。
        真实调用链是 `_merge_tag_plan(infer_tags_structured(..., tag_hints), tag_hints)`。
        """
        hints = ["#custom-x"]
        plan = zot._merge_tag_plan(
            zot.infer_tags_structured("demo alpha thing", tag_hints=hints,
                                      vocab=_synthetic_vocab()),
            hints,
        )
        assert "#custom-x" in plan["children"]
        assert "#custom-x" in plan["new"], f"new 漏报：{plan['new']}"
        assert "#demo-alpha" not in plan["new"], "库内既有的 tag 不该算新建"

    def test_existing_root_not_reported_as_new(self, zot):
        plan = zot._merge_tag_plan(
            zot.infer_tags_structured("demo alpha thing", vocab=_synthetic_vocab())
        )
        assert plan["root"] == "/demo📦"
        assert "/demo📦" not in plan["new"]


# ---------------------------------------------------------------------------
# C-1: auto-only 降权只能打破平局，不能当阈值闸门
# ---------------------------------------------------------------------------

class TestAutoOnlyPenaltyIsNotAGate:
    """旧版 CLI 写的 tag 全是 type 1，于是用户的策展词表在词表里全带 auto_only。

    若用降权后的 score 过 `_ROOT_SCORE_THRESHOLD=2.0`：
        2.0（segment 强度）× 1.1（prior）× 0.5（auto 惩罚）= 1.1 < 2.0
    库里有 `/machine-learning📚`、标题也完全对得上，却被判定"没有"，
    转手新建一个 root —— 正是本 PR 要消灭的发散。
    """

    slug = "machine-learning"

    def _vocab(self, types):
        v = _synthetic_vocab()
        v["roots"] = [{"tag": f"/{self.slug}📚", "slug": self.slug, "n": 9,
                       "types": list(types), "type": types[0],
                       "status": False, "children_count": 0}]
        v["children"] = {}
        return v

    def test_manual_vocab_matches_existing_root(self, zot):
        plan = zot.infer_tags_structured("Machine learning for engineers",
                                         vocab=self._vocab([0]))
        assert plan["root"] == "/machine-learning📚"
        assert plan["mode"] == "existing"

    def test_auto_only_vocab_matches_the_same_root(self, zot):
        """同一份词表，只把 type 从 0 改成 1 —— root 选择必须不变。"""
        manual = zot.infer_tags_structured("Machine learning for engineers",
                                           vocab=self._vocab([0]))
        auto = zot.infer_tags_structured("Machine learning for engineers",
                                         vocab=self._vocab([1]))
        assert auto["root"] == manual["root"], (
            f"auto-only 把 root 判定打翻了：auto={auto['root']} manual={manual['root']}"
        )
        assert auto["mode"] == "existing", (
            "存量（auto-only）tag 必须能被复用，不能被降权打到阈值以下"
        )

    def test_penalty_still_breaks_ties(self, zot):
        """降权没有消失：同强度同热度时，manual 仍排在 auto-only 之前。"""
        v = _synthetic_vocab()
        v["roots"] = [
            {"tag": "/alpha📚", "slug": "alpha", "n": 9, "types": [1],
             "type": 1, "status": False, "children_count": 0},
            {"tag": "/bravo📚", "slug": "bravo", "n": 9, "types": [0],
             "type": 0, "status": False, "children_count": 0},
        ]
        v["children"] = {}
        plan = zot.infer_tags_structured("alpha bravo", vocab=v)
        assert plan["root"] == "/bravo📚", f"平局时应选 manual，实得 {plan['root']}"


# ---------------------------------------------------------------------------
# I-1: tag 写入一律 type 0（manual）
# ---------------------------------------------------------------------------

class TestTagWritesAreAlwaysManual:
    def _stub(self, zot, monkeypatch, vocab_types):
        captured = {}

        def fake_item(key):
            return {"key": key, "data": {"title": "t", "tags": []}}

        def fake_update(item):
            captured["item"] = item
            return True

        # `_tags_update` 调的是模块级 client 对象 `zot.zot`（pyzotero.Zotero 实例）
        monkeypatch.setattr(zot.zot, "item", fake_item)
        monkeypatch.setattr(zot.zot, "update_item", fake_update)
        monkeypatch.setattr(zot, "_vocab_tag_types",
                            lambda vocab=None: dict(vocab_types))
        return captured

    def test_add_writes_type_zero_even_for_auto_only_tags(self, zot, monkeypatch):
        """`/unread` 在词表里是 auto-only，但写进库时必须是 manual。

        修之前：`_type_for` 沿用词表观测 type → 写 type 1 →
        仍会被「Delete Automatic Tags in This Library…」一键清空。
        """
        captured = self._stub(zot, monkeypatch, {"/unread": 1, "#demo-alpha": 1})
        zot._tags_update("K1", ["/unread", "#demo-alpha"], "add")

        written = {t["tag"]: t["type"] for t in captured["item"]["data"]["tags"]}
        assert written == {"/unread": 0, "#demo-alpha": 0}, written
