"""Multi-signal Zotero collection classification engine.

Architecture (see /var/minis/workspace/zot-classify-spec.md for full spec):

  Layer 0: Public API  ──→ find_best_collection(title, description)
  Layer 1: Signal extraction (6 sources, graceful degradation)
  Layer 2: Collection corpus signatures (TF-IDF)
  Layer 3: Weighted scoring
  Layer 4: Confidence gate
  Layer 5: Fallback chain

PR status:
  PR 1: Refactor + zero behavior change (MERGED, v2.5.0).
    - Layer 3 (scoring): replicates current find_best_collection logic
    - Layer 2 (signature): not implemented
    - Layer 4 (gate): not implemented (no-op)
    - Layer 5 (fallback): not implemented (returns None when scoring fails)
  PR 2 (current): Multi-signal extraction.
    - Layer 1 (extract_signals): 6 sources implemented
    - Layer 3 (scoring): STILL the PR 1 text-keyword logic — signals are
      extracted and reported but do NOT yet change the decision
    - New: `zot explain` CLI subcommand prints the signal table
  PR 3: Will add corpus signatures
  PR 4: Will add confidence gate + fallback chain (signals start deciding)
  PR 5: Will add negative-example memory

Public API contract:
    find_best_collection(title, description) → (coll_key, coll_name) | None
    PR 1/2: behavior MUST be identical to the pre-refactor version.
    PR 4+: behavior changes by design (gate + fallback).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Constants (forward-looking; not all used in PR 1)
# ---------------------------------------------------------------------------

# Source weight in Layer 3 weighted scoring (see spec §4.1).
# Higher weight = more reliable signal. Tuning comes after PR 4 telemetry.
SOURCE_WEIGHTS = {
    "user_hint":   1.00,
    "domain":      0.70,
    "url_path":    0.50,
    "body":        0.40,
    "description": 0.20,
    "title":       0.15,
    "tag":         0.10,
}

# Minimum items before a corpus signature is built (small colls skip it).
MIN_ITEMS_FOR_SIGNATURE = 5

# Confidence gate thresholds (PR 4 will enforce these).
MIN_BEST_SCORE = 0.50
MIN_SIGNAL_SOURCES = 2
MIN_MARGIN = 0.15

# TTL for collection signature cache.
SIGNATURE_TTL_SECONDS = 3600

# URL path patterns → (collection_hint, confidence). First match wins.
# Ordered most-specific-first.
#
# Patterns are matched against "<netloc><path>" (e.g. "zhihu.com/question/123"),
# NOT the path alone — otherwise "/question/123" (zhihu) would be swallowed by
# the stackoverflow pattern "/questions?/\d", since "questions?" also matches
# the singular "question".
#
# Domain-specific paths keep the netloc in the pattern. Only the two generic
# Substack shapes ("/post/", "/p/") stay netloc-agnostic, because a custom
# domain on Substack is still a Substack post.
URL_PATH_PATTERNS: tuple[tuple[str, str, float], ...] = (
    (r"(^|\.)arxiv\.org/(abs|pdf)/\d",        "arxiv",        0.95),
    (r"(^|\.)youtube\.com/watch|^youtu\.be/", "youtube",      0.90),
    (r"(^|\.)stackoverflow\.com/questions?/\d", "stackoverflow", 0.90),
    (r"(^|\.)wikipedia\.org/wiki/",            "wikipedia",    0.90),
    (r"(^|\.)ycombinator\.com/item",           "hn",           0.90),
    (r"(^|\.)bilibili\.com/video/BV",          "bilibili",     0.85),
    (r"(^|\.)xiaohongshu\.com/discovery/item/", "xhs",          0.85),
    (r"(^|\.)zhihu\.com/question/\d",          "zhihu",        0.80),
    (r"/post/",                                "substack",     0.70),
    (r"/p/",                                   "substack",     0.60),
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleInput:
    """Normalized article data passed into classification.

    PR 2 populates url/body_text/user_hints when the caller has them.
    """
    url: str = ""
    title: str = ""
    description: str = ""
    body_text: Optional[str] = None
    user_hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Signal:
    """A single piece of evidence about classification."""
    source: str                        # 'user_hint' | 'domain' | 'url_path' | 'body' | 'description' | 'title' | 'tag'
    collection_hint: Optional[str]      # Suggested coll name (e.g., 'Misc--hn') or None
    confidence: float                  # 0.0..1.0
    evidence: str                      # Human-readable explanation


@dataclass(frozen=True)
class CollectionSignature:
    """TF-IDF weighted keywords from items in a collection (PR 3)."""
    coll_key: str
    coll_name: str
    keywords: dict[str, float]         # keyword → weight
    item_count: int
    built_at: float                    # Unix timestamp for TTL


@dataclass(frozen=True)
class ClassificationDecision:
    """Result of full classification pipeline."""
    chosen: Optional[tuple[str, str]]   # (coll_key, coll_name) | None
    score: float                         # 0..1 confidence
    runner_up: Optional[tuple[str, str]]
    runner_up_score: float
    signals: list[Signal]
    signature_used: bool                # Did corpus signature contribute? (PR 3+)
    fallback_used: Optional[str]        # Which fallback tier fired (PR 4+)
    rejected_reasons: list[str]         # Why other candidates lost (PR 4+)
    debug_log: list[str]                # Step-by-step trace


# ---------------------------------------------------------------------------
# Layer 1: Signal extraction (PR 2)
# ---------------------------------------------------------------------------

def _signal_user_hint(article: ArticleInput) -> Optional[Signal]:
    """User-supplied hints — the most reliable signal when present."""
    hints = [h for h in (article.user_hints or []) if h and h.strip()]
    if not hints:
        return None
    return Signal(
        source="user_hint",
        collection_hint=hints[0],
        confidence=0.99,
        evidence=f"user hint: {', '.join(hints[:3])}",
    )


def _signal_domain(article: ArticleInput) -> Optional[Signal]:
    """URL netloc → known platform, via zot.py's DOMAIN_TO_SUBCOLL."""
    if not article.url:
        return None
    try:
        import zot
        sub = zot._domain_subcoll_name(article.url)
    except Exception:  # noqa: BLE001 — signals must never raise
        return None
    if not sub:
        return None
    return Signal(
        source="domain",
        collection_hint=f"Misc--{sub}",
        confidence=0.90,
        evidence=f"URL domain → {sub}",
    )


def _signal_url_path(article: ArticleInput) -> Optional[Signal]:
    """URL path shape → platform (covers custom domains on known platforms)."""
    if not article.url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(article.url)
        # Match against netloc+path, not path alone — see URL_PATH_PATTERNS.
        target = (parsed.netloc or "") + (parsed.path or "")
    except Exception:  # noqa: BLE001
        return None
    for pattern, hint, confidence in URL_PATH_PATTERNS:
        if re.search(pattern, target):
            return Signal(
                source="url_path",
                collection_hint=f"Misc--{hint}",
                confidence=confidence,
                evidence=f"URL {target!r} matches {pattern!r} → {hint}",
            )
    return None


def _signal_body(article: ArticleInput) -> Optional[Signal]:
    """Full article body — richest text signal, but only when available."""
    body = (article.body_text or "").strip()
    if len(body) < 80:
        return None
    keywords = _top_keywords(body, limit=6)
    if not keywords:
        return None
    return Signal(
        source="body",
        collection_hint=None,          # PR 3 will match against coll signatures
        confidence=0.40,
        evidence=f"body keywords: {', '.join(keywords)}",
    )


def _signal_description(article: ArticleInput) -> Optional[Signal]:
    """Meta description — often generic, so low weight."""
    desc = (article.description or "").strip()
    if len(desc) < 20:
        return None
    keywords = _top_keywords(desc, limit=5)
    if not keywords:
        return None
    return Signal(
        source="description",
        collection_hint=None,
        confidence=0.20,
        evidence=f"description keywords: {', '.join(keywords)}",
    )


def _signal_title(article: ArticleInput) -> Optional[Signal]:
    """Article title — short, so low weight, but always present."""
    title = (article.title or "").strip()
    if not title or title.lower().startswith("http"):
        return None
    keywords = _top_keywords(title, limit=4)
    if not keywords:
        return None
    return Signal(
        source="title",
        collection_hint=None,
        confidence=0.15,
        evidence=f"title keywords: {', '.join(keywords)}",
    )


def _top_keywords(text: str, limit: int = 5) -> list[str]:
    """Top content words of a text (reuses zot.py's stopword-filtered extractor)."""
    try:
        import zot
        words = zot._title_keywords(text)
    except Exception:  # noqa: BLE001
        return []
    # Drop very short / numeric tokens, keep the longest (most specific) first
    useful = [w for w in words if len(w) >= 4 and not w.isdigit()]
    return sorted(useful, key=len, reverse=True)[:limit]


def extract_signals(article: ArticleInput) -> list[Signal]:
    """Extract all available signals from an article.

    PR 2: 6 sources, each independent — one failing never blocks the others.
    Sources are returned in descending SOURCE_WEIGHTS order so the most
    reliable signal comes first in the diagnostic output.

    Never raises; returns [] on any error. Caller must handle empty case.
    """
    extractors = (
        _signal_user_hint,
        _signal_domain,
        _signal_url_path,
        _signal_body,
        _signal_description,
        _signal_title,
    )
    signals: list[Signal] = []
    for extractor in extractors:
        try:
            sig = extractor(article)
        except Exception:  # noqa: BLE001 — a broken extractor must not
            continue                   # take down the whole pipeline
        if sig is not None:
            signals.append(sig)
    signals.sort(key=lambda s: -SOURCE_WEIGHTS.get(s.source, 0.0))
    return signals


def format_signals(signals: list[Signal]) -> str:
    """Render the signal table for `zot explain` output."""
    if not signals:
        return "  (no signals extracted)"
    lines = []
    for sig in signals:
        hint = sig.collection_hint or "-"
        lines.append(
            f"  {sig.source:<12} conf={sig.confidence:.2f}  hint={hint:<24} {sig.evidence}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2: Collection signature cache (PR 1: just the cache structure)
# ---------------------------------------------------------------------------

_signature_cache: dict[str, CollectionSignature] = {}


def invalidate_signature_cache(coll_key: Optional[str] = None) -> None:
    """Invalidate one coll's signature or all."""
    if coll_key is None:
        _signature_cache.clear()
    else:
        _signature_cache.pop(coll_key, None)


def get_cached_signature(coll_key: str, coll_name: str) -> Optional[CollectionSignature]:
    """Return cached signature or build a new one. PR 3 will implement build."""
    sig = _signature_cache.get(coll_key)
    if sig is not None and (time.time() - sig.built_at) < SIGNATURE_TTL_SECONDS:
        return sig
    # PR 1: signature building is not implemented; returns None.
    return None


# ---------------------------------------------------------------------------
# Layer 3: Scoring (PR 1: replicates current find_best_collection logic)
# ---------------------------------------------------------------------------

def _default_keyword_helpers():
    """Lazily fetch zot.py's keyword helpers.

    Only used when the caller did not pass pre-computed keywords. The CLI path
    (zot.find_best_collection) always passes all three, so zot.py is never
    imported a second time there — `python3 scripts/zot.py …` runs it as
    __main__, and a plain `import zot` would load a second copy with its own
    Zotero client and its own collection cache.
    """
    import zot  # noqa: WPS433 — intentional lazy import
    return zot._extract_text_keywords, zot._extract_collection_keywords, zot._is_url


def score_collection(
    coll: tuple[str, str],
    article: ArticleInput,
    signals: list[Signal],
    text_keywords: Optional[set[str]] = None,
    extract_coll_keywords=None,
    url_title_guard=None,
) -> tuple[int, float, list[str]]:
    """Score one collection against signals.

    PR 1: signals are ignored. Scoring uses title+description text directly,
    mirroring the existing find_best_collection behavior.

    Why BOTH the count and the normalized score are returned: ordering MUST use
    the raw count. The legacy implementation compared counts, so "9 matches"
    beats "6 matches" — but once the count is capped at 5 both normalize to 1.0
    and the comparison degenerates into "whichever came first". The 0..1 score
    is for display and for PR 4's confidence gate only.

    Args:
        text_keywords: pre-computed keywords for the article text. Computing
            this per candidate was a ~6x CPU regression, so the caller computes
            it once. Omitted → derived lazily (tests only).
        extract_coll_keywords: callable (name → keyword set). Omitted → lazy
            import from zot.py (tests only).
        url_title_guard: callable (title → bool), i.e. zot._is_url. Omitted →
            lazy import (tests only).

    Returns: (intersection_count, score 0..1, debug trace lines)
    """
    key, name = coll

    if text_keywords is None or extract_coll_keywords is None or url_title_guard is None:
        default_text_kw, default_coll_kw, default_is_url = _default_keyword_helpers()
        if text_keywords is None:
            text = (article.title + " " + article.description).lower()
            text_keywords = default_text_kw(text)
        extract_coll_keywords = extract_coll_keywords or default_coll_kw
        url_title_guard = url_title_guard or default_is_url

    # Replicate early-return condition: title is URL and description is empty
    if url_title_guard(article.title) and not article.description.strip():
        return 0, 0.0, ["skip: title is URL and description empty"]

    coll_keywords = extract_coll_keywords(name)
    intersection_count = len(coll_keywords & text_keywords)

    # Normalize to 0..1 scale (display / gate only — never used for ordering).
    # 0 → 0.0; 1 → 0.2; 2 → 0.4; 3 → 0.6; 4 → 0.8; 5+ → 1.0
    if intersection_count == 0:
        score = 0.0
    else:
        score = min(intersection_count, 5) / 5.0

    trace = [f"intersection_count={intersection_count}, score={score:.2f}"]
    return intersection_count, score, trace


# ---------------------------------------------------------------------------
# Layer 0 + orchestrator: classify()
# ---------------------------------------------------------------------------

def classify(
    article: ArticleInput,
    candidates: Optional[list[tuple[str, str]]] = None,
    text_keywords: Optional[set[str]] = None,
    extract_coll_keywords=None,
    url_title_guard=None,
) -> ClassificationDecision:
    """Run full classification pipeline.

    PR 1: extract_signals() returns []. Scoring uses legacy text-based logic.
    Behavior is identical to the pre-refactor find_best_collection when
    `candidates` defaults to fetching from Zotero.

    Args:
        article: input article data
        candidates: optional pre-fetched (key, name) list. If None, fetches
                    via zot._all_collections() filtered for forbidden + Misc.
        text_keywords: pre-computed keywords for the article text (computed
                       once by the caller; see score_collection).
        extract_coll_keywords: callable (name → keyword set).
        url_title_guard: callable (title → bool), i.e. zot._is_url.

    Returns:
        ClassificationDecision with `chosen` field matching the legacy
        find_best_collection return shape (tuple | None).
    """
    signals = extract_signals(article)
    debug_log: list[str] = [f"signals: {len(signals)} extracted"]

    # Resolve the keyword helpers. The CLI path (zot.find_best_collection)
    # passes all three explicitly, so `import zot` never runs there — which is
    # what keeps zot.py from being loaded a second time as a separate module.
    if text_keywords is None or extract_coll_keywords is None or url_title_guard is None:
        default_text_kw, default_coll_kw, default_is_url = _default_keyword_helpers()
        if text_keywords is None:
            text_keywords = default_text_kw(
                (article.title + " " + article.description).lower()
            )
        extract_coll_keywords = extract_coll_keywords or default_coll_kw
        url_title_guard = url_title_guard or default_is_url

    # Legacy early return, before any API work: URL title + empty description
    if url_title_guard(article.title) and not article.description.strip():
        debug_log.append("chosen: None (title is URL, description empty)")
        return ClassificationDecision(
            chosen=None, score=0.0, runner_up=None, runner_up_score=0.0,
            signals=signals, signature_used=False, fallback_used=None,
            rejected_reasons=[], debug_log=debug_log,
        )

    # Build candidate list (only reached when the caller did not supply one)
    if candidates is None:
        import zot
        candidates = []
        for c in zot._all_collections():
            key = c["key"]
            if key in zot._get_forbidden_collection_keys():
                continue
            if key == zot.MISC_COLLECTION:
                continue
            candidates.append((key, c["data"].get("name", "")))

    # Score each, tracking max by RAW intersection count (legacy compared
    # counts; the normalized score saturates at 5 matches and must not decide).
    best_coll: Optional[tuple[str, str]] = None
    best_count = 0
    best_score = 0.0
    best_trace: list[str] = []
    runner_up: Optional[tuple[str, str]] = None
    runner_up_score = 0.0

    for coll in candidates:
        count, score, trace = score_collection(
            coll, article, signals,
            text_keywords=text_keywords,
            extract_coll_keywords=extract_coll_keywords,
            url_title_guard=url_title_guard,
        )
        debug_log.append(f"  {coll[1]:<50}  {count:>2} hit  {score:.2f}  ({trace[0]})")

        # Strict-greater tiebreak matches legacy `score > best_score` semantics
        if count >= 1 and count > best_count:
            runner_up = best_coll
            runner_up_score = best_score
            best_coll = coll
            best_count = count
            best_score = score
            best_trace = trace
        elif count >= 1 and score > runner_up_score:
            runner_up = coll
            runner_up_score = score

    # Legacy behavior: any positive overlap → chosen
    chosen = best_coll if best_count >= 1 else None

    if chosen and best_trace:
        debug_log.append(
            f"chosen: {chosen[1]} ({best_count} hits, score={best_score:.2f}, {best_trace[0]})"
        )
    else:
        debug_log.append("chosen: None (no keyword overlap)")

    return ClassificationDecision(
        chosen=chosen,
        score=best_score,
        runner_up=runner_up,
        runner_up_score=runner_up_score,
        signals=signals,
        signature_used=False,
        fallback_used=None,
        rejected_reasons=[],
        debug_log=debug_log,
    )


# ---------------------------------------------------------------------------
# Convenience wrapper for the public API
# ---------------------------------------------------------------------------

def classify_from_title_desc(title: str, description: str) -> ClassificationDecision:
    """Shorthand for callers that don't yet have URL/body/hints (PR 1 callers)."""
    return classify(ArticleInput(url="", title=title, description=description))