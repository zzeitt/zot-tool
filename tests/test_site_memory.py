"""Unit tests for v2.6.0 site memory + browser header helpers.

Pure logic only — no network, no Zotero API, no claude-mem worker.
"""
import json
import os
import sqlite3
import subprocess

import pytest


_OBS_SCHEMA = """
create table observations (
  id integer primary key,
  memory_session_id text not null,
  project text not null,
  text text,
  type text not null,
  title text,
  subtitle text,
  facts text,
  narrative text,
  concepts text,
  files_read text,
  files_modified text,
  prompt_number integer,
  created_at text not null,
  created_at_epoch integer not null,
  discovery_tokens integer default 0,
  content_hash text,
  generated_by_model text,
  relevance_count integer default 0,
  merged_into_project text,
  agent_type text,
  agent_id text,
  metadata text,
  synced_at integer,
  origin_device_id text,
  origin_local_id text,
  sync_rev text not null default '1',
  occurrence_count integer not null default 1,
  title_norm_key text,
  reinforcement_dates text,
  last_reinforced text
);
"""


@pytest.fixture
def mem_db(zot, tmp_path, monkeypatch):
    """Point site memory at a throwaway claude-mem-shaped DB."""
    path = tmp_path / "claude-mem.db"
    conn = sqlite3.connect(path)
    conn.executescript(_OBS_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(zot, "_site_mem_db", lambda: str(path))
    monkeypatch.setattr(zot, "_site_mem_file", lambda: str(tmp_path / "sites.json"))
    monkeypatch.setattr(zot, "_site_mem_post", lambda payload: False)
    monkeypatch.setenv("ZOTERO_SITE_MEM", "claude-mem")
    return str(path)


@pytest.fixture
def file_store(zot, tmp_path, monkeypatch):
    """Force the JSON-file backend."""
    path = tmp_path / "sites.json"
    monkeypatch.setattr(zot, "_site_mem_file", lambda: str(path))
    monkeypatch.setenv("ZOTERO_SITE_MEM", "file")
    return path


# ---------------------------------------------------------------------------
# URL / CSS helpers
# ---------------------------------------------------------------------------

def test_site_domain(zot):
    assert zot._site_domain("https://developer.arm.com/x/y") == "developer.arm.com"
    assert zot._site_domain("http://Example.COM:8080/a") == "example.com:8080"
    assert zot._site_domain("not a url") == ""


def test_absolutize_css_urls(zot):
    css = 'body{background:url("../img/a.png")} @font-face{src:url(f.woff2)}'
    out = zot._absolutize_css_urls(css, "https://x.test/css/main.css")
    assert "https://x.test/img/a.png" in out
    assert "https://x.test/css/f.woff2" in out


def test_inline_css_imports_follows_import(zot, monkeypatch):
    pages = {
        "https://cdn.test/ds.css": ".ds{color:red}",
    }
    monkeypatch.setattr(zot, "_curl_get_text",
                        lambda url, timeout=30, extra_args=None: pages.get(url))
    css = '@import"https://cdn.test/ds.css";.own{margin:0}'
    out = zot._inline_css_imports(css, "https://x.test/a.css")
    assert ".ds{color:red}" in out
    assert ".own{margin:0}" in out
    assert "@import" not in out


def test_inline_stylesheets_replaces_links(zot, monkeypatch):
    pages = {"https://x.test/a.css": ".a{padding:1px}"}
    monkeypatch.setattr(zot, "_curl_get_text",
                        lambda url, timeout=30, extra_args=None: pages.get(url))
    html = ('<html><head><link rel="stylesheet" href="/a.css">'
            '</head><body>hi</body></html>')
    out = zot._inline_stylesheets(html, "https://x.test/post/1")
    assert '<link rel="stylesheet"' not in out
    assert ".a{padding:1px}" in out
    assert "<style>" in out


def test_looks_blocked(zot):
    assert zot._looks_blocked("<html><title>Access Denied</title>")
    assert zot._looks_blocked("<html>Attention Required! | Cloudflare")
    assert not zot._looks_blocked("<html><body>Gappa part 1</body>")


# ---------------------------------------------------------------------------
# Request headers
# ---------------------------------------------------------------------------

def test_curl_get_text_sends_sec_fetch_headers(zot, monkeypatch):
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, b"<html>ok</html>", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = zot._curl_get_text("https://x.test/", timeout=5)
    assert out == "<html>ok</html>"
    joined = " ".join(seen["args"])
    for token in ("Sec-Fetch-Dest: document", "Sec-Fetch-Mode: navigate",
                  "Upgrade-Insecure-Requests: 1", "Accept-Language:"):
        assert token in joined


# ---------------------------------------------------------------------------
# Site profile store
# ---------------------------------------------------------------------------

def test_profile_roundtrip_direct_fallback(zot, mem_db):
    zot._site_profile_put("https://developer.arm.com/x", offline="curl_css",
                          collection="demo0001", note="monolith 403")
    prof = zot._site_profile_get("https://developer.arm.com/other")
    assert prof["offline"] == "curl_css"
    assert prof["collection"] == "demo0001"
    assert prof["note"] == "monolith 403"
    assert prof["domain"] == "developer.arm.com"


def test_profile_put_updates_instead_of_appending(zot, mem_db):
    zot._site_profile_put("https://a.test/1", offline="monolith")
    zot._site_profile_put("https://a.test/2", collection="ABC123")
    conn = sqlite3.connect(mem_db)
    n = conn.execute("select count(*) from observations "
                     "where title='zot-site:a.test'").fetchone()[0]
    conn.close()
    assert n == 1
    prof = zot._site_profile_get("https://a.test/")
    assert prof["offline"] == "monolith" and prof["collection"] == "ABC123"


def test_profile_missing_returns_none(zot, mem_db):
    assert zot._site_profile_get("https://never-seen.test/") is None


def test_profiles_all_dedupes_by_domain(zot, mem_db):
    zot._site_profile_put("https://a.test/", offline="monolith")
    zot._site_profile_put("https://b.test/", offline="curl_css")
    domains = {p["domain"] for p in zot._site_profiles_all()}
    assert domains == {"a.test", "b.test"}


def test_site_memory_disabled_is_a_noop(zot, tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_SITE_MEM", "off")
    monkeypatch.setattr(zot, "_site_mem_db",
                        lambda: str(tmp_path / "missing.db"))
    monkeypatch.setattr(zot, "_site_mem_file",
                        lambda: str(tmp_path / "sites.json"))
    assert zot._site_profile_get("https://x.test/") is None
    zot._site_profile_put("https://x.test/", offline="monolith")
    assert zot._site_profiles_all() == []


# ---------------------------------------------------------------------------
# Backend selection + JSON-file fallback (works without claude-mem)
# ---------------------------------------------------------------------------

def test_mode_auto_prefers_claude_mem(zot, tmp_path, monkeypatch):
    db = tmp_path / "claude-mem.db"
    db.write_text("")
    monkeypatch.setenv("ZOTERO_SITE_MEM", "auto")
    monkeypatch.setattr(zot, "_site_mem_db", lambda: str(db))
    assert zot._site_mem_mode() == "claude-mem"


def test_mode_auto_falls_back_to_file(zot, tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_SITE_MEM", "auto")
    monkeypatch.setattr(zot, "_site_mem_db",
                        lambda: str(tmp_path / "absent.db"))
    assert zot._site_mem_mode() == "file"


def test_mode_off(zot, monkeypatch):
    monkeypatch.setenv("ZOTERO_SITE_MEM", "off")
    assert zot._site_mem_mode() == "off"


def test_file_backend_roundtrip(zot, file_store):
    zot._site_profile_put("https://a.test/x", offline="curl_css",
                          collection="demo0001")
    prof = zot._site_profile_get("https://a.test/y")
    assert prof["offline"] == "curl_css"
    assert prof["collection"] == "demo0001"
    assert prof["domain"] == "a.test"
    assert file_store.exists()


def test_file_backend_updates_in_place(zot, file_store):
    zot._site_profile_put("https://a.test/1", offline="monolith")
    zot._site_profile_put("https://a.test/2", note="second pass")
    prof = zot._site_profile_get("https://a.test/")
    assert prof["offline"] == "monolith" and prof["note"] == "second pass"
    assert set(json.loads(file_store.read_text())) == {"a.test"}
