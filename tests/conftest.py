"""zot-tool test suite — shared fixtures and configuration.

Prerequisites:
  - ZOTERO_API_KEY — API key with read/write access to your test library
  - ZOTERO_TEST_LIBRARY_ID — your test library ID (required, no default)
  - ZOTERO_TEST_LIBRARY_TYPE — "group" or "user" (default: "group")

Test isolation:
  - Prerequisite collections (Forbidden, Misc) created once per session
  - Per-test items tracked and auto-deleted on teardown
  - Empty collections left for future test runs
"""

import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import pytest


# ---------------------------------------------------------------------------
# Session-scoped: env setup + prerequisite collections
# ---------------------------------------------------------------------------
# Users can point tests at their own Zotero group library via env vars:
#   ZOTERO_TEST_LIBRARY_ID   — group/user library ID (required)
#   ZOTERO_TEST_LIBRARY_TYPE — "group" or "user" (default: "group")
#
# To set up a test library:
#   1. Create a group at https://www.zotero.org/groups (or use a user library)
#   2. Generate an API key with read/write access at https://www.zotero.org/settings/keys
#   3. Set ZOTERO_API_KEY + ZOTERO_TEST_LIBRARY_ID (and optionally ZOTERO_TEST_LIBRARY_TYPE)
#   4. Run: python -m pytest tests/ -v
#
# The test suite auto-creates prerequisite collections (Forbidden + Misc)
# and cleans up all test-created items on teardown. No manual config needed.

TEST_LIBRARY_ID = os.environ.get("ZOTERO_TEST_LIBRARY_ID")
TEST_LIBRARY_TYPE = os.environ.get("ZOTERO_TEST_LIBRARY_TYPE", "group")
# We create these in the test library if they don't exist
FORBIDDEN_COLL_NAME = os.environ.get("ZOTERO_TEST_FORBIDDEN_NAME", "Test-Forbidden")
MISC_COLL_NAME = os.environ.get("ZOTERO_TEST_MISC_NAME", "Test-Misc")

# These get filled by setup_collections fixture
_forbidden_key = None
_misc_key = None


@pytest.fixture(scope="session")
def test_env(tmp_path_factory):
    """Set env vars for test group library (session-scoped, runs once).

    Uses os.environ directly (not monkeypatch) so imports after this fixture
    see the test values.

    ZOTERO_VOCAB_DIR points at a throwaway directory: the vocab/domain-overlay
    caches hold data derived from whichever library the tests hit, and must
    never land in (or be read from) the developer's real cache.
    """
    saved = {}
    overrides = {
        "ZOTERO_LIBRARY_ID": TEST_LIBRARY_ID,
        "ZOTERO_LIBRARY_TYPE": TEST_LIBRARY_TYPE,
        "ZOTERO_VOCAB_DIR": str(tmp_path_factory.mktemp("zot_vocab")),
    }
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        if v is not None:
            os.environ[k] = v

    yield overrides

    # Restore original values
    for k, orig in saved.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig


@pytest.fixture(scope="session")
def setup_collections(test_env):
    """Create prerequisite collections in test library.

    Creates (if not exist):
      - Test-Forbidden — placeholder forbidden collection
      - Test-Misc — parent for Misc--* subcollections

    Also sets ZOTERO_FORBIDDEN_COLLECTION + ZOTERO_MISC_COLLECTION env vars.

    Yields (forbidden_key, misc_key). Cleans up on teardown.
    """
    global _forbidden_key, _misc_key

    from pyzotero import zotero as zotlib

    api_key = os.environ.get("ZOTERO_API_KEY", "")
    if not api_key:
        pytest.fail("ZOTERO_API_KEY not set — cannot access test library")

    zot = zotlib.Zotero(TEST_LIBRARY_ID, TEST_LIBRARY_TYPE, api_key)

    # --- Find or create Test-Forbidden ---
    forbidden_key = None
    try:
        for c in zot.everything(zot.collections()):
            if c["data"].get("name") == FORBIDDEN_COLL_NAME:
                forbidden_key = c["key"]
                break
    except Exception as e:
        pytest.fail(f"Cannot list collections in test library: {e}")

    if not forbidden_key:
        try:
            resp = zot.create_collections([{"name": FORBIDDEN_COLL_NAME}])
            forbidden_key = resp["successful"]["0"]["key"]
            print(f"\n[setup] Created {FORBIDDEN_COLL_NAME}: {forbidden_key}")
        except Exception as e:
            pytest.fail(f"Cannot create {FORBIDDEN_COLL_NAME}: {e}")
    else:
        print(f"\n[setup] Found existing {FORBIDDEN_COLL_NAME}: {forbidden_key}")

    # --- Find or create Test-Misc ---
    misc_key = None
    try:
        for c in zot.everything(zot.collections()):
            if c["data"].get("name") == MISC_COLL_NAME:
                misc_key = c["key"]
                break
    except Exception as e:
        pytest.fail(f"Cannot list collections: {e}")

    if not misc_key:
        try:
            resp = zot.create_collections([{"name": MISC_COLL_NAME}])
            misc_key = resp["successful"]["0"]["key"]
            print(f"[setup] Created {MISC_COLL_NAME}: {misc_key}")
        except Exception as e:
            pytest.fail(f"Cannot create {MISC_COLL_NAME}: {e}")
    else:
        print(f"[setup] Found existing {MISC_COLL_NAME}: {misc_key}")

    # Set env vars for zot.py
    os.environ["ZOTERO_FORBIDDEN_COLLECTION"] = forbidden_key
    os.environ["ZOTERO_MISC_COLLECTION"] = misc_key

    _forbidden_key = forbidden_key
    _misc_key = misc_key

    yield forbidden_key, misc_key

    # Teardown: delete prerequisite collections (and all their contents)
    print("\n[teardown] Cleaning up test collections...")
    api_base = f"https://api.zotero.org/{TEST_LIBRARY_TYPE}s/{TEST_LIBRARY_ID}"
    for ck, cname in [(forbidden_key, FORBIDDEN_COLL_NAME),
                       (misc_key, MISC_COLL_NAME)]:
        try:
            # Get version for If-Unmodified-Since-Version
            import requests
            r = requests.get(
                f"{api_base}/collections/{ck}",
                headers={"Authorization": f"Bearer {api_key}",
                         "Zotero-API-Version": "3"},
            )
            if r.status_code == 200:
                ver = (r.json().get("version")
                       or r.json().get("data", {}).get("version"))
                if ver:
                    requests.delete(
                        f"{api_base}/collections/{ck}",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Zotero-API-Version": "3",
                            "If-Unmodified-Since-Version": str(ver),
                        },
                    )
                    print(f"[teardown] Deleted {cname}: {ck}")
        except Exception as e:
            print(f"[teardown] Warning: could not delete {cname}: {e}")

    # Clean up env vars
    os.environ.pop("ZOTERO_FORBIDDEN_COLLECTION", None)
    os.environ.pop("ZOTERO_MISC_COLLECTION", None)


@pytest.fixture(scope="session")
def zot_mod(test_env, setup_collections):
    """Import zot.py with test env vars set. Session-scoped — one import.

    Returns the zot module object. Call its functions directly in tests.
    """
    # Ensure scripts/ is on path
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    # The env vars were set by test_env + setup_collections fixtures above.
    # Import zot module — its top-level code runs with test values.
    import zot as zot_module
    return zot_module


@pytest.fixture(scope="module")
def zot():
    """Import zot.py with dummy env vars (no API access).

    For pure-logic tests (URL detection, emoji mapping, image compression).
    zot.py's module-level code only needs these vars to exist; the Zotero
    client is lazy and makes no calls until used.
    """
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    saved = {}
    overrides = {
        "ZOTERO_API_KEY": "dummy-key",
        "ZOTERO_LIBRARY_ID": "000000",
        "ZOTERO_FORBIDDEN_COLLECTION": "DUMMYFORBIDDEN",
        "ZOTERO_MISC_COLLECTION": "DUMMYMISC",
    }
    for k, v in overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    import zot as zot_module
    yield zot_module

    for k, orig in saved.items():
        if orig is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = orig


@pytest.fixture(scope="session")
def api_client(test_env, setup_collections):
    """Raw pyzotero client for API-level verification in tests."""
    from pyzotero import zotero
    api_key = os.environ.get("ZOTERO_API_KEY", "")
    return zotero.Zotero(TEST_LIBRARY_ID, TEST_LIBRARY_TYPE, api_key)


# ---------------------------------------------------------------------------
# Function-scoped: per-test item tracking + cleanup
# ---------------------------------------------------------------------------

@pytest.fixture
def tracked_items(zot_mod, api_client):
    """Track created items and auto-delete on test teardown.

    Yields a list. Append item keys to it during tests.
    On teardown, deletes all tracked items.
    """
    keys = []
    yield keys
    # Teardown: delete all tracked items
    for key in keys:
        try:
            items = api_client.item(key)
            item = items[0] if isinstance(items, list) else items
            api_client.delete_item(item)
        except Exception as e:
            print(f"[teardown] Warning: could not delete item {key}: {e}")


@pytest.fixture
def misc_key(setup_collections):
    """Return the Test-Misc collection key for tests that need it."""
    return setup_collections[1]


@pytest.fixture
def forbidden_key(setup_collections):
    """Return the Test-Forbidden collection key."""
    return setup_collections[0]


# ---------------------------------------------------------------------------
# Durable trace note — proves integration tests hit the real Zotero library
# ---------------------------------------------------------------------------
# The suite is otherwise self-cleaning (everything created is deleted on
# teardown), which makes it hard to confirm the tests really reached the API.
# This hook leaves ONE persistent note per run in a dedicated collection so
# there's an audit trail. It only fires when ZOTERO_TEST_LIBRARY_ID +
# ZOTERO_API_KEY are present (the CI "Integration tests" step, or a local
# full-suite run) — unit runs (test_unit.py / test_image_compress.py) skip it.

CI_TRACE_COLLECTION = "CI-Test-Runs"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args):
    """Run git in the repo root; "" when git or the repo is unavailable."""
    try:
        out = subprocess.run(
            ["git", "-C", _REPO_ROOT] + list(args),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def skill_version():
    """Version under test. SKILL.md frontmatter is the single source of truth.

    The auto-release workflow tags releases from this same line, so reading it
    back is what ties a trace note to a released version rather than to a sha
    the reader has to resolve by hand.
    """
    try:
        with open(os.path.join(_REPO_ROOT, "SKILL.md"), encoding="utf-8") as fh:
            for _ in range(40):
                line = fh.readline()
                if not line:
                    break
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip() or "unknown"
    except OSError:
        pass
    return "unknown"


def run_identity():
    """Describe exactly which revision was tested.

    Without this a trace note only says "51/51 passed" and there is no way to
    tell which patch that verdict belongs to.
    """
    env = os.environ

    # On a pull_request event GITHUB_SHA is the synthetic merge commit and
    # GITHUB_REF is refs/pull/<N>/merge; HEAD^2 is the real PR tip — the code
    # actually under test. Locally neither exists, so fall back to HEAD.
    pr = ""
    match = re.match(r"refs/pull/(\d+)/", env.get("GITHUB_REF", ""))
    if match:
        pr = match.group(1)

    sha = env.get("GITHUB_SHA") or _git("rev-parse", "HEAD") or "n/a"
    commit = _git("rev-parse", "HEAD^2") if pr else ""
    if not commit:
        commit = _git("rev-parse", "HEAD") or sha

    branch = (env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME")
              or _git("rev-parse", "--abbrev-ref", "HEAD") or "n/a")

    # A local run on a dirty tree tests code no commit contains; say so rather
    # than letting the commit hash masquerade as the tested revision.
    dirty = bool(_git("status", "--porcelain"))
    diff = _git("diff", "HEAD") if dirty else ""

    return {
        "version": skill_version(),
        "commit": commit[:10] if commit != "n/a" else "n/a",
        "subject": _git("log", "-1", "--format=%s", commit) or "n/a",
        "branch": branch,
        "pr": pr,
        "sha": sha,
        "dirty": dirty,
        "diff_hash": hashlib.sha1(diff.encode("utf-8")).hexdigest()[:8]
                     if diff else "",
    }


def build_trace_note(ident, counts, status, now, run_id):
    """Render the trace note body. Pure — no API, no environment."""
    passed, failed, errors, skipped, total = counts
    headline = (f"CI integration run — {passed}/{total} passed ({status})")
    if ident["version"] != "unknown":
        headline += f" — v{ident['version']}"
    if ident["commit"] != "n/a":
        headline += f" @ {ident['commit']}"
    if ident["dirty"]:
        headline += " (dirty tree)"

    lines = [
        f"<li>version: {ident['version']}</li>",
        f"<li>commit: {ident['commit']} — {ident['subject']}</li>",
        f"<li>branch: {ident['branch']}"
        + (f" · PR #{ident['pr']}" if ident["pr"] else "") + "</li>",
        f"<li>sha: {ident['sha']}</li>",
        f"<li>run: {run_id}</li>",
        f"<li>time: {now}</li>",
        f"<li>passed: {passed} / failed: {failed} / errors: {errors}"
        f" / skipped: {skipped}</li>",
    ]
    if ident["dirty"]:
        lines.append(
            "<li>tree: DIRTY — uncommitted changes were tested; the commit "
            f"above is the base only (diff {ident['diff_hash']})</li>")
    return f"<p>{headline}</p>\n<ul>\n" + "\n".join(lines) + "\n</ul>"


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Leave a durable trace note recording the integration run result."""
    if not os.environ.get("ZOTERO_TEST_LIBRARY_ID") \
            or not os.environ.get("ZOTERO_API_KEY"):
        return  # unit run or no API configured — nothing to trace

    passed = len(terminalreporter.stats.get("passed", []))
    failed = len(terminalreporter.stats.get("failed", []))
    errors = len(terminalreporter.stats.get("error", []))
    skipped = len(terminalreporter.stats.get("skipped", []))
    total = passed + failed + errors + skipped

    try:
        from pyzotero import zotero

        zot = zotero.Zotero(
            TEST_LIBRARY_ID, TEST_LIBRARY_TYPE, os.environ["ZOTERO_API_KEY"])

        # Find or create the persistent collection (never cleaned up)
        coll_key = None
        for c in zot.everything(zot.collections()):
            if c["data"].get("name") == CI_TRACE_COLLECTION:
                coll_key = c["key"]
                break
        if not coll_key:
            resp = zot.create_collections([{"name": CI_TRACE_COLLECTION}])
            coll_key = resp["successful"]["0"]["key"]

        now = datetime.now(timezone.utc).isoformat()
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        status = "OK" if exitstatus == 0 else f"FAILED (exit {exitstatus})"

        ident = run_identity()
        note_html = build_trace_note(
            ident, (passed, failed, errors, skipped, total), status, now, run_id)
        resp = zot.create_items([{
            "itemType": "note",
            "note": note_html,
            "collections": [coll_key],
        }])
        # pyzotero create_items only raises HTTP errors, not the "failed"
        # field — a silently rejected note (HTTP 200 + failed) would otherwise
        # print success. Mirror zot.py's _create_note check.
        if not isinstance(resp, dict):
            print(f"\n[trace] Warning: unexpected note response {resp!r}")
            return
        if resp.get("failed"):
            msgs = []
            for entry in resp["failed"].values():
                msgs.append(str(entry.get("message") or entry)
                             if isinstance(entry, dict) else str(entry))
            print(f"\n[trace] Warning: note rejected: {'; '.join(msgs)}")
            return
        where = f"v{ident['version']} @ {ident['commit']}"
        if ident["dirty"]:
            where += " (dirty)"
        print(f"\n[trace] Left run note in '{CI_TRACE_COLLECTION}': "
              f"{passed}/{total} passed ({status}) — {where}")
    except Exception as e:
        # Never fail the run because of the trace itself
        print(f"\n[trace] Warning: could not leave trace note: {e}")
