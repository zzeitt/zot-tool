"""Regression tests — 词表落盘不得用 rename 覆盖（2026-11-25）。

**事故形态**：Minis / iSH 的文件删除保护（FileGuard）把 "rename 覆盖已存在
文件" 当成删除。``_write_vocab_file`` 原先用 tmp + ``os.replace`` 落盘到
``$ZOTERO_VOCAB_DIR/tags.json``（默认落在受保护目录
``/var/minis/shared/zot_vocab/``），于是**每次刷新词表都弹**
"把旧文件移到回收站？"确认框。用户点"不允许" → 写入被拒 →
``generated_ts`` 永远不更新 → 下次仍判定词表过期 → 再拉 API → 再弹窗，
变成自锁循环。

**修复**：``_write_vocab_file`` 改为原地写（``open(path, "w")``）——
改内容既不是 unlink 也不是 rename-over，不触发保护。

⚠️ 本文件**不使用** conftest 的 ``zot`` / ``zot_mod`` fixture：
两者导入的是**同一个** ``sys.modules["zot"]`` key，谁先跑谁定身份
（见 test_collections_cache.py 的详细说明）。这里用**隔离的模块实例**：
按文件路径 exec 一份 ``zot.py``，模块名 ``zot_isolated``。
"""

import importlib.util
import json
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
ZOT_PY = os.path.join(SCRIPTS_DIR, "zot.py")


def _load_isolated_zot():
    """exec 一份独立的 zot.py，模块名不与 sys.modules 冲突。"""
    saved = {}
    overrides = {
        "ZOTERO_API_KEY": "dummy-key",
        "ZOTERO_LIBRARY_ID": "000000",
        "ZOTERO_LIBRARY_TYPE": "user",
        "ZOTERO_FORBIDDEN_COLLECTION": "DUMMYFORBIDDEN",
        "ZOTERO_MISC_COLLECTION": "DUMMYMISC",
    }
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        spec = importlib.util.spec_from_file_location("zot_isolated", ZOT_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig


@pytest.fixture(scope="module")
def zot():
    """隔离的 zot 模块实例 —— 不污染 sys.modules['zot']。"""
    return _load_isolated_zot()


class TestVocabWriteIsNotRename:
    """词表写盘不得走 rename 覆盖（会触发 FileGuard 删除保护弹窗）。"""

    def test_write_does_not_use_os_replace(self, zot, tmp_path, monkeypatch):
        """os.replace 被换成毒丸后，写盘仍须成功。"""
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path / "zot_vocab"))

        def _poison(*a, **kw):  # pragma: no cover - 触发即失败
            raise AssertionError(
                "_write_vocab_file 用了 os.replace —— rename 覆盖已存在文件"
                "会被 Minis FileGuard 当成删除并弹回收站确认框"
            )

        monkeypatch.setattr(zot.os, "replace", _poison)

        assert zot._write_vocab_file({"generated_ts": 123.0, "roots": []}) is True

        out = zot._read_vocab_file()
        assert out is not None
        assert out["generated_ts"] == 123.0

    def test_write_replaces_existing_file_in_place(self, zot, tmp_path, monkeypatch):
        """已有词表文件时也能覆盖写，且不产生 .tmp 残留。"""
        vocab_dir = tmp_path / "zot_vocab"
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(vocab_dir))
        vocab_dir.mkdir(parents=True)
        target = vocab_dir / "tags.json"
        target.write_text(json.dumps({"generated_ts": 1.0, "roots": []}),
                          encoding="utf-8")

        assert zot._write_vocab_file({"generated_ts": 2.0, "roots": []}) is True

        out = json.loads(target.read_text(encoding="utf-8"))
        assert out["generated_ts"] == 2.0
        assert not (vocab_dir / "tags.json.tmp").exists(), \
            "不应再产生 .tmp 中间文件（那是 rename-over 的残留）"

    def test_invalid_payload_does_not_raise(self, zot, tmp_path, monkeypatch):
        """不可序列化的载荷必须静默失败，而不是炸掉归档。"""
        monkeypatch.setenv("ZOTERO_VOCAB_DIR", str(tmp_path / "zot_vocab"))
        assert zot._write_vocab_file({"bad": object()}) is False


class TestIsolation:
    """本文件不得影响其它测试的模块身份。"""

    def test_does_not_pollute_sys_modules(self, zot):
        assert "zot_isolated" not in sys.modules or \
            sys.modules.get("zot_isolated") is zot
        real = sys.modules.get("zot")
        if real is not None:
            assert real is not zot
