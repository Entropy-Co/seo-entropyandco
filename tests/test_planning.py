from __future__ import annotations

import pytest

from entropyseo.agents import content_strategy
from entropyseo.agents.keyword_research import Candidate, classify_intent
from entropyseo.agents.title_quality import natural_editorial_title, search_keyword_from_phrase
from entropyseo.agents.topic_dedupe import find_duplicate, related_titles
from entropyseo.schemas import ContentStrategyDecisionDraft
from entropyseo.tools.corpus_tools import _strip_category_labels

EXISTING = [
    "Human-in-the-Loop Automation: 7 Approval Patterns That Actually Ship",
    "AI Marketing Agency USA Mega Guide: Ads, SEO, Email, and Automation",
    "Marketing Attribution for Small Business: 5 Sanity Checks Before You Trust Any Number",
    "AI Receptionist for Law Firms: Scripts, Guardrails, and Real Costs",
]


@pytest.mark.parametrize(
    ("term", "duplicate"),
    [
        ("human in the loop ai agents", EXISTING[0]),
        ("ai cmo", None),
        ("ai marketing automation for small business", None),
        ("ai automation for law firms", None),
        ("n8n vs custom ai agents", None),
    ],
)
def test_find_duplicate_uses_headline_subject(term: str, duplicate: str | None) -> None:
    assert find_duplicate(term, EXISTING) == duplicate


def test_related_titles_excludes_near_duplicates() -> None:
    pairs = [(t, f"https://entropyand.co/blog/{i}") for i, t in enumerate(EXISTING)]
    related = related_titles("AI Receptionist for Dental Offices: Costs and Scripts", pairs)
    assert related and related[0][0] == EXISTING[3]


def test_strip_category_labels() -> None:
    titles = {f"s{i}": f"Article Title Number {i}" for i in range(8)} | {"x": "Resources The 5-Tool AI Stack"}
    stripped = _strip_category_labels(titles)
    assert stripped["s0"] == "Title Number 0"
    assert stripped["x"] == "The 5-Tool AI Stack"


def test_keyword_helpers() -> None:
    assert (
        search_keyword_from_phrase("How do I automate client reporting for my agency?")
        == "i automate client reporting for my agency"
    )
    assert classify_intent("make vs n8n for agencies") == "commercial"
    assert not natural_editorial_title("N8n Vs Custom Ai Agents", "n8n vs custom ai agents")
    assert natural_editorial_title("n8n vs Custom AI Agents: When Each One Pays Off", "n8n vs custom ai agents")


def _cand(term: str, score: float) -> Candidate:
    return Candidate(
        term=term,
        intent="commercial",
        pillar="AI for agencies",
        sources=["niche_priority"],
        evidence=["seed"],
        gsc_impressions=0,
        gsc_position=None,
        score=score,
    )


def test_strategy_rejects_pick_outside_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pick = ContentStrategyDecisionDraft(
        decision="proceed", title="Something Invented Entirely New Here", primary_keyword="invented keyword"
    )
    monkeypatch.setattr(content_strategy, "_llm_pick", lambda *a, **k: pick)
    result = content_strategy.run(candidates=[_cand("make vs n8n for agencies", 5.0)], existing_titles=EXISTING)
    assert result["decision"] == "proceed"
    assert result["calendar_candidate"]["primary_keyword"] == "make vs n8n for agencies"


def test_strategy_uses_llm_title_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    pick = ContentStrategyDecisionDraft(
        decision="proceed",
        title="Make vs n8n for Agencies: Which Scales With Client Count",
        primary_keyword="make vs n8n for agencies",
        content_type="comparison",
        buyer_stage="decision",
    )
    monkeypatch.setattr(content_strategy, "_llm_pick", lambda *a, **k: pick)
    result = content_strategy.run(candidates=[_cand("make vs n8n for agencies", 5.0)], existing_titles=EXISTING)
    assert result["calendar_candidate"]["title"].startswith("Make vs n8n for Agencies")
    assert result["calendar_candidate"]["content_type"] == "comparison"


def test_strategy_skips_when_everything_is_covered() -> None:
    result = content_strategy.run(
        candidates=[_cand("human in the loop ai agents", 9.0)], existing_titles=EXISTING, use_llm=False
    )
    assert result["decision"] == "skip"
