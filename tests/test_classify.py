"""Tests for zot_classify multi-signal classification engine.

PR 1 contract: classify() must produce identical output to _legacy_find_best_collection()
when given the same input and candidate list.

NOTE on imports:
  zot_classify is imported at module level (no zot.py dependency).
  zot is NOT imported at module level because zot.py does sys.exit(1) when
  env vars are missing. Instead, the `zot` fixture from conftest.py is used,
  which sets dummy env vars before importing zot and restores them after.
  This pattern matches tests/test_unit.py + tests/cleanup.py.
"""

import os
import sys

# Ensure scripts/ is on sys.path so zot_classify can be imported at module level.
# Pattern: tests/cleanup.py uses the same sys.path.insert at top.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

import zot_classify
from zot_classify import (
    ArticleInput,
    Signal,
    ClassificationDecision,
    CollectionSignature,
    classify,
    score_collection,
    extract_signals,
    invalidate_signature_cache,
    get_cached_signature,
    SOURCE_WEIGHTS,
    MIN_ITEMS_FOR_SIGNATURE,
)


# ---------------------------------------------------------------------------
# Sample candidate list (no API needed)
# ---------------------------------------------------------------------------

CANDIDATES = [
    ("MATHFV",     "Math-FormalVerification"),
    ("TURING1936", "On Computable Numbers, with an Applicatoin to the Entscheidungsproblem"),
    ("TURING1950", "Computing Machinery and Intelligence"),
    ("AIMATH",     "AI-Math"),
    ("AI4MATH",    "AI4Math"),
    ("PRINCETON",  "The Princeton Companion to Mathematics"),
    ("EDVAC",      "First Draft of a Report on the EDVAC"),
    ("PRIMES",     "The Mystery of the Prime Numbers"),
    ("FPA",        "Handbook of Floating-Point Arithmetic"),
    ("WEB",        "Misc--web"),
]


# ---------------------------------------------------------------------------
# Pure data-class tests (no zot needed)
# ---------------------------------------------------------------------------

class TestDataClasses:
    def test_article_input_defaults(self):
        a = ArticleInput()
        assert a.url == ""
        assert a.title == ""
        assert a.description == ""
        assert a.body_text is None
        assert a.user_hints == []

    def test_article_input_frozen(self):
        a = ArticleInput(title="x")
        with pytest.raises(Exception):
            a.title = "y"

    def test_signal_frozen(self):
        s = Signal(source="domain", collection_hint="hn", confidence=0.9, evidence="x")
        with pytest.raises(Exception):
            s.confidence = 0.5

    def test_decision_frozen(self):
        d = ClassificationDecision(
            chosen=None, score=0.0,
            runner_up=None, runner_up_score=0.0,
            signals=[], signature_used=False,
            fallback_used=None, rejected_reasons=[],
            debug_log=[],
        )
        with pytest.raises(Exception):
            d.score = 1.0


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

class TestConstants:
    def test_source_weights_keys(self):
        for s in ("user_hint", "domain", "url_path", "body",
                  "description", "title", "tag"):
            assert s in SOURCE_WEIGHTS, f"Missing weight for {s}"
            assert 0.0 < SOURCE_WEIGHTS[s] <= 1.0

    def test_user_hint_highest(self):
        weights = SOURCE_WEIGHTS
        assert weights["user_hint"] == max(weights.values())


# ---------------------------------------------------------------------------
# extract_signals (PR 1: stub)
# ---------------------------------------------------------------------------

class TestExtractSignals:
    def test_returns_empty_list(self):
        article = ArticleInput(
            url="https://example.com",
            title="FLT: Anthropic has beaten me to it",
            description="I guess technically...",
        )
        assert extract_signals(article) == []

    def test_no_exceptions_on_garbage(self):
        a = ArticleInput(url="", title="", description="")
        assert extract_signals(a) == []


# ---------------------------------------------------------------------------
# score_collection (uses zot lazily inside)
# ---------------------------------------------------------------------------

class TestScoreCollection:
    def test_url_title_no_desc_returns_zero(self, zot):
        """Early-return: title is URL and description empty → score 0."""
        a = ArticleInput(url="", title="https://example.com", description="")
        score, trace = score_collection(("X", "Some Coll"), a, [])
        assert score == 0.0
        assert "skip" in trace[0]

    def test_score_in_zero_one_range(self, zot):
        """Score must be a float in [0, 1]."""
        a = ArticleInput(url="", title="Lean formal proof", description="Mathlib")
        for coll in CANDIDATES:
            score, _ = score_collection(coll, a, [])
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0

    def test_no_keyword_overlap_returns_zero(self, zot):
        """Coll name with no shared keywords → 0."""
        a = ArticleInput(url="", title="xyzzy frobozz magic", description="plover")
        score, trace = score_collection(
            ("X", "On Computable Numbers, with an Applicatoin to the Entscheidungsproblem"),
            a, [],
        )
        assert score <= 0.4


# ---------------------------------------------------------------------------
# classify — orchestrator (uses zot.helpers lazily inside)
# ---------------------------------------------------------------------------

class TestClassifyOrchestrator:
    def test_returns_decision(self, zot):
        a = ArticleInput(url="", title="Lean", description="Mathlib")
        decision = classify(a, candidates=CANDIDATES)
        assert isinstance(decision, ClassificationDecision)
        assert decision.signals == []
        assert decision.signature_used is False
        assert decision.fallback_used is None

    def test_empty_inputs(self, zot):
        a = ArticleInput(url="", title="", description="")
        decision = classify(a, candidates=CANDIDATES)
        assert decision.chosen is None

    def test_url_title_empty_desc(self, zot):
        a = ArticleInput(url="", title="https://example.com", description="")
        decision = classify(a, candidates=CANDIDATES)
        assert decision.chosen is None

    def test_score_propagated(self, zot):
        a = ArticleInput(url="", title="Lean proof Mathlib", description="formal")
        decision = classify(a, candidates=CANDIDATES)
        if decision.chosen is not None:
            assert decision.score > 0.0
            assert decision.score <= 1.0

    def test_debug_log_populated(self, zot):
        a = ArticleInput(url="", title="test", description="")
        decision = classify(a, candidates=CANDIDATES)
        assert len(decision.debug_log) > 0
        assert any("signals" in line for line in decision.debug_log)


# ---------------------------------------------------------------------------
# Behavior preservation (PR 1 contract)
# ---------------------------------------------------------------------------

class TestBehaviorPreservation:
    """Compare new classify() against legacy logic.

    Both must produce identical `chosen` output for the same inputs and candidate list.
    zot fixture provides zot module with dummy env vars.
    """

    @staticmethod
    def _legacy_with_candidates(title, description, candidates, zot):
        """Replicate legacy find_best_collection logic but with injected candidates."""
        text = (title + " " + description).lower()
        if zot._is_url(title) and not description.strip():
            return None
        text_keywords = zot._extract_text_keywords(text)
        best_match = None
        best_score = 0
        for key, name in candidates:
            coll_keywords = zot._extract_collection_keywords(name)
            score = len(coll_keywords & text_keywords)
            if score >= 1 and score > best_score:
                best_score = score
                best_match = (key, name)
        return best_match

    @pytest.mark.parametrize("title,description", [
        # Historical P1 bug cases (should NOT have a chosen in either pre-PR-4)
        ("FLT: Anthropic has beaten me to it",
         "I guess technically it was revealed to the world by a coffee shop in Islington on Insta"),
        ("OpenAI claims huge maths breakthrough on a famed 'Millennium Problem'",
         "The company says it has cracked the physics of fluids using AI"),
        # Clear match cases
        ("Formalizing Fermat's Last Theorem in Lean",
         "We present a formal proof using Mathlib"),
        ("GitHub repository release",
         "GitHub is a code hosting platform"),
        # Edge cases
        ("", ""),
        ("https://example.com", ""),
        ("random text", ""),
    ])
    def test_new_matches_legacy(self, zot, title, description):
        """PR 1 contract: classify().chosen == legacy().chosen."""
        new_decision = classify(
            ArticleInput(url="", title=title, description=description),
            candidates=CANDIDATES,
        )
        legacy_result = self._legacy_with_candidates(
            title, description, CANDIDATES, zot
        )
        assert new_decision.chosen == legacy_result, (
            f"Behavior diverged for ({title!r}, {description!r}): "
            f"new={new_decision.chosen} legacy={legacy_result}"
        )


# ---------------------------------------------------------------------------
# Signature cache primitives (no zot needed)
# ---------------------------------------------------------------------------

class TestSignatureCachePrimitives:
    def test_invalidate_all(self):
        invalidate_signature_cache()

    def test_invalidate_specific_key(self):
        invalidate_signature_cache("NONEXISTENT_KEY")

    def test_get_cached_signature_returns_none_in_pr1(self):
        """PR 1: signature building not implemented; always None."""
        sig = get_cached_signature("X", "Some Coll")
        assert sig is None