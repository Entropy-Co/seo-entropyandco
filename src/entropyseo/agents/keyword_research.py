"""keyword-research agent: build a scored keyword candidate pool.

Scoring is deterministic Python so rankings are reproducible and auditable; the
LLM (fast tier) only clusters candidates and writes a one-line strategic summary.
No volume numbers are invented: evidence is the list of real signals that
surfaced a term (niche seed, GSC impressions/position, SERP related searches, PAA).

Pipeline:
  1. Seeds     niche priority keywords (or pillars), plus an optional seed topic
  2. Discover  SerpAPI related searches + People Also Ask; GSC near-rank queries
  3. Classify  rule-based intent + pillar assignment
  4. Score     source weight x intent weight + pillar fit + GSC demand
  5. Cluster   LLM groups the top candidates into hub/spoke clusters (optional)
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, TypedDict

from entropyseo.agents.title_quality import queueable_keyword, search_keyword_from_phrase
from entropyseo.agents.topic_dedupe import topic_tokens
from entropyseo.config import load_niche_config
from entropyseo.llm import dumps, inject_niche_context, load_prompt, structured
from entropyseo.schemas import KeywordResearchDraft
from entropyseo.tools import search_tools

logger = logging.getLogger(__name__)

PROMPT = "keyword-research"
VERSION = "v1.0"
MAX_SERP_SEEDS = 6
MAX_CLUSTER_CANDIDATES = 60

_SOURCE_WEIGHTS = {
    "gsc_near_rank": 4.0,
    "niche_priority": 3.0,
    "people_also_ask": 2.0,
    "related_search": 1.5,
    "gsc_query": 1.5,
}
_INTENT_WEIGHTS = {"commercial": 1.3, "transactional": 1.2, "informational": 1.0, "navigational": 0.4}
_COMMERCIAL_RE = re.compile(
    r"\b(best|top|vs|versus|alternative|alternatives|compare|comparison|cost|costs|pricing|price|agency|agencies|"
    r"consultant|consulting|service|services|software|platform|tool|tools|review|reviews|roi)\b",
    re.I,
)
_TRANSACTIONAL_RE = re.compile(r"\b(hire|buy|book|quote|demo|get started|near me)\b", re.I)
_NAVIGATIONAL_RE = re.compile(r"\b(login|sign in|download|app store|github)\b", re.I)


class Candidate(TypedDict):
    term: str
    intent: str
    pillar: str | None
    sources: list[str]
    evidence: list[str]
    gsc_impressions: int
    gsc_position: float | None
    score: float


def classify_intent(term: str) -> str:
    if _NAVIGATIONAL_RE.search(term):
        return "navigational"
    if _TRANSACTIONAL_RE.search(term):
        return "transactional"
    if _COMMERCIAL_RE.search(term):
        return "commercial"
    return "informational"


def assign_pillar(term: str) -> str | None:
    """Pillar whose name/description/seed keywords share the most canonical tokens with the term."""
    niche = load_niche_config()
    term_tokens = topic_tokens(term)
    best: tuple[int, str | None] = (0, None)
    for pillar in niche.pillars:
        vocab = topic_tokens(f"{pillar.name} {pillar.description}")
        vocab |= {t for kw in niche.priority_keywords if kw.pillar == pillar.name for t in topic_tokens(kw.term)}
        overlap = len(term_tokens & vocab)
        if overlap > best[0]:
            best = (overlap, pillar.name)
    return best[1]


def _add(pool: dict[str, Candidate], term: str, source: str, evidence: str, **extra: Any) -> None:
    keyword = search_keyword_from_phrase(term)
    if not queueable_keyword(keyword):
        return
    row = pool.get(keyword)
    if row is None:
        row = Candidate(
            term=keyword,
            intent=classify_intent(keyword),
            pillar=extra.get("pillar") or assign_pillar(keyword),
            sources=[],
            evidence=[],
            gsc_impressions=0,
            gsc_position=None,
            score=0.0,
        )
        pool[keyword] = row
    if source not in row["sources"]:
        row["sources"].append(source)
    if evidence and evidence not in row["evidence"]:
        row["evidence"].append(evidence)
    if extra.get("impressions"):
        row["gsc_impressions"] = max(row["gsc_impressions"], int(extra["impressions"]))
    if extra.get("position") is not None:
        position = float(extra["position"])
        row["gsc_position"] = position if row["gsc_position"] is None else min(row["gsc_position"], position)


def score_candidate(row: Candidate) -> float:
    source_score = max((_SOURCE_WEIGHTS.get(s, 1.0) for s in row["sources"]), default=1.0)
    multi_source_bonus = 0.5 * (len(row["sources"]) - 1)
    pillar_bonus = 1.0 if row["pillar"] else -1.0
    demand = math.log10(row["gsc_impressions"] + 1) if row["gsc_impressions"] else 0.0
    return round((source_score + multi_source_bonus + pillar_bonus + demand) * _INTENT_WEIGHTS[row["intent"]], 2)


def discover(seed_topic: str | None = None) -> list[Candidate]:
    niche = load_niche_config()
    pool: dict[str, Candidate] = {}
    for kw in niche.priority_keywords:
        _add(pool, kw.term, "niche_priority", f"niche priority keyword ({kw.intent})", pillar=kw.pillar)
    seeds = ([seed_topic] if seed_topic else []) + niche.keyword_seeds()
    for seed in list(dict.fromkeys(seeds))[:MAX_SERP_SEEDS]:
        expansion = search_tools.serpapi_expand(seed)
        for related in expansion["related_searches"]:
            _add(pool, related, "related_search", f"Google related search for '{seed}'")
        for question in expansion["people_also_ask"]:
            _add(pool, question, "people_also_ask", f"People Also Ask: {question}")
    gsc_rows = search_tools.gsc_queries()
    near_rank = {r["query"] for r in search_tools.gsc_near_rank(gsc_rows)}
    for row in gsc_rows:
        source = "gsc_near_rank" if row["query"] in near_rank else "gsc_query"
        _add(
            pool,
            row["query"],
            source,
            f"GSC: {row['impressions']} impressions, avg position {row['position']:.1f}",
            impressions=row["impressions"],
            position=row["position"],
        )
    banned = [t.lower() for t in niche.banned_topics]
    candidates = [c for c in pool.values() if not any(b in c["term"] for b in banned)]
    for candidate in candidates:
        candidate["score"] = score_candidate(candidate)
    return sorted(candidates, key=lambda r: r["score"], reverse=True)


def cluster(candidates: list[Candidate]) -> KeywordResearchDraft:
    if not candidates:
        return KeywordResearchDraft()
    payload = {
        "candidates": [
            {"term": c["term"], "intent": c["intent"], "pillar": c["pillar"], "score": c["score"]}
            for c in candidates[:MAX_CLUSTER_CANDIDATES]
        ],
        "pillars": load_niche_config().pillar_names(),
    }
    try:
        return structured(
            system_prompt=load_prompt(PROMPT, VERSION),
            user_content=inject_niche_context(dumps(payload), include_voice=False),
            schema=KeywordResearchDraft,
            tier="fast",
            max_tokens=4000,
        )
    except Exception as exc:
        logger.warning("keyword clustering failed; continuing without clusters: %s", exc)
        return KeywordResearchDraft()


def run(seed_topic: str | None = None, *, with_clusters: bool = True) -> dict[str, Any]:
    candidates = discover(seed_topic)
    research = cluster(candidates) if with_clusters else KeywordResearchDraft()
    return {
        "candidates": candidates,
        "clusters": [c.model_dump(mode="json") for c in research.clusters],
        "strategic_headline": research.strategic_headline,
        "gap_notes": research.gap_notes,
        "signals": {
            "serpapi": search_tools.serpapi_available(),
            "gsc_rows": sum(1 for c in candidates if c["gsc_impressions"]),
        },
    }
