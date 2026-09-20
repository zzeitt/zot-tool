"""Multi-signal Zotero collection classification engine.

Architecture (see /var/minis/workspace/zot-classify-spec.md for full spec):

  Layer 0: Public API  ──→ find_best_collection(title, description)
  Layer 1: Signal extraction (6 sources, graceful degradation)
  Layer 2: Collection corpus signatures (TF-IDF)
  Layer 3: Weighted scoring
  Layer 4: Confidence gate
  Layer 5: Fallback chain

PR status:
  PR 1 (current): Refactor + zero behavior change.
    - Layer 1 (extract_signals): stub returning []
    - Layer 2 (signature): not implemented
    - Layer 3 (scoring): replicates current find_best_collection logic
    - Layer 4 (gate): not implemented (no-op)
    - Layer 5 (fallback): not implemented (returns None when scoring fails)
  PR 2: Will populate extract_signals with 6 sources
  PR 3: Will add corpus signatures
  PR 4: Will add confidence gate + fallback chain
  PR 5: Will add negative-example memory

Public API contract (PR 1):
    find_best_collection(title, description) → (coll_key, coll_name) | None
    Behavior MUST be identical to the pre-refactor version.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleInput:
    """Normalized article data passed into classification.

    PR 1 only uses title+description. URL/body/user_hints are populated in PR 2+.
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
# Layer 1: Signal extraction
# ---------------------------------------------------------------------------

def extract_signals(article: ArticleInput) -> list[Signal]:
    """Extract all available signals from an article.

    PR 1: stub returning empty list. PR 2 will populate from 6 sources.

    Never raises; returns [] on any error. Caller must handle empty case.
    """
    return []


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

def score_collection(
    coll: tuple[str, str],
    article: ArticleInput,
    signals: list[Signal],
) -> tuple[float, list[str]]:
    """Score one collection against signals.

    PR 1: signals are ignored. Scoring uses title+description text directly,
    mirroring the existing find_best_collection behavior:
      1. Early return 0.0 if title is URL and description is empty
      2. Compute keyword intersection between text and coll name
      3. Normalize count to 0..1 (1 match=0.2, 5+=1.0) for PR 4 gate compat

    Returns: (score 0..1, debug trace lines)
    """
    # Lazy import to avoid circular dependency with zot.py
    import zot

    key, name = coll

    # Replicate early-return condition: title is URL and description is empty
    text = (article.title + " " + article.description).lower()
    if zot._is_url(article.title) and not article.description.strip():
        return 0.0, ["skip: title is URL and description empty"]

    # Replicate existing scoring: keyword intersection count
    text_keywords = zot._extract_text_keywords(text)
    coll_keywords = zot._extract_collection_keywords(name)
    intersection_count = len(coll_keywords & text_keywords)

    # Normalize to 0..1 scale.
    # 0 → 0.0; 1 → 0.2; 2 → 0.4; 3 → 0.6; 4 → 0.8; 5+ → 1.0
    # PR 4 gate threshold 0.5 → needs ≥3 keyword matches (or fallback tier)
    if intersection_count == 0:
        score = 0.0
    else:
        score = min(intersection_count, 5) / 5.0

    trace = [f"intersection_count={intersection_count}, score={score:.2f}"]
    return score, trace


# ---------------------------------------------------------------------------
# Layer 0 + orchestrator: classify()
# ---------------------------------------------------------------------------

def classify(
    article: ArticleInput,
    candidates: Optional[list[tuple[str, str]]] = None,
) -> ClassificationDecision:
    """Run full classification pipeline.

    PR 1: extract_signals() returns []. Scoring uses legacy text-based logic.
    Behavior is identical to the pre-refactor find_best_collection when
    `candidates` defaults to fetching from Zotero.

    Args:
        article: input article data
        candidates: optional pre-fetched (key, name) list. If None, fetches
                    via zot._all_collections() filtered for forbidden + Misc.

    Returns:
        ClassificationDecision with `chosen` field matching the legacy
        find_best_collection return shape (tuple | None).
    """
    import zot

    signals = extract_signals(article)

    # Build candidate list
    if candidates is None:
        candidates = []
        for c in zot._all_collections():
            key = c["key"]
            if key in zot._get_forbidden_collection_keys():
                continue
            if key == zot.MISC_COLLECTION:
                continue
            candidates.append((key, c["data"].get("name", "")))

    # Score each, tracking max (with strict-greater tiebreak for parity)
    best_coll: Optional[tuple[str, str]] = None
    best_score = 0.0
    best_trace: list[str] = []
    runner_up: Optional[tuple[str, str]] = None
    runner_up_score = 0.0

    debug_log: list[str] = [f"signals: {len(signals)} extracted"]

    for coll in candidates:
        score, trace = score_collection(coll, article, signals)
        debug_log.append(f"  {coll[1]:<50}  {score:.2f}  ({trace[0]})")

        # Strict-greater tiebreak matches legacy `score > best_score` semantics
        if score > 0.0 and score > best_score:
            runner_up = best_coll
            runner_up_score = best_score
            best_coll = coll
            best_score = score
            best_trace = trace
        elif score > 0.0 and score > runner_up_score:
            runner_up = coll
            runner_up_score = score

    # Legacy behavior: any positive score → chosen
    chosen = best_coll if best_score > 0.0 else None

    if chosen and best_trace:
        debug_log.append(f"chosen: {chosen[1]} (score={best_score:.2f}, {best_trace[0]})")
    else:
        debug_log.append("chosen: None (no match above threshold)")

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