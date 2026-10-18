"""content-strategy agent: pick the next article (or skip) from the candidate pool.

Deterministic filtering does the heavy lifting; the LLM only chooses among
pre-vetted candidates and writes the headline and angle.

  1. Filter  drop banned topics and anything the live site, Framer, the ledger,
             or the calendar already covers (topic-family dedupe)
  2. Rank    keyword-research score, boosted by trend overlap and cluster hubs
  3. Pick    LLM chooses one of the top N and writes title + angle
  4. Guard   the pick must be in the pool and its title must pass quality and
             dedupe checks; otherwise fall back to the top deterministic row
"""

from __future__ import annotations

import logging
from typing import Any

from entropyseo.agents.keyword_research import Candidate
from entropyseo.agents.title_quality import fallback_title, natural_editorial_title
from entropyseo.agents.topic_dedupe import find_duplicate, topic_core_tokens, topic_family_id
from entropyseo.config import load_niche_config
from entropyseo.llm import dumps, inject_niche_context, load_prompt, structured
from entropyseo.schemas import CalendarCandidate, ContentStrategyDecisionDraft, TrendSignal

logger = logging.getLogger(__name__)

PROMPT = "content-strategy"
VERSION = "v1.0"
TOP_N_FOR_LLM = 15


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def filter_candidates(
    candidates: list[Candidate], existing_titles: list[str]
) -> tuple[list[Candidate], list[dict[str, str]]]:
    kept: list[Candidate] = []
    rejected: list[dict[str, str]] = []
    for row in candidates:
        duplicate = find_duplicate(row["term"], existing_titles)
        if duplicate:
            rejected.append({"term": row["term"], "reason": f"covered by existing article: {duplicate}"})
            continue
        kept.append(row)
    return kept, rejected


def rank(candidates: list[Candidate], trends: list[TrendSignal], clusters: list[dict[str, Any]]) -> list[Candidate]:
    # Core tokens only: "ai" and "agent" alone would match nearly every AI story on Hacker News.
    trend_tokens = [topic_core_tokens(t.title) for t in trends[:20]]
    hub_terms = {_normalize(str(c.get("hub_term", ""))) for c in clusters}
    ranked: list[Candidate] = []
    for row in candidates:
        tokens = topic_core_tokens(row["term"])
        boost = 0.0
        evidence = list(row["evidence"])
        if any(len(tokens & t) >= 2 or (len(tokens) == 1 and tokens <= t) for t in trend_tokens):
            boost += 1.0
            evidence.append("overlaps a recent high-engagement Hacker News discussion")
        if _normalize(row["term"]) in hub_terms:
            boost += 0.75
        ranked.append(Candidate(**{**row, "evidence": evidence, "score": round(row["score"] + boost, 2)}))
    return sorted(ranked, key=lambda r: r["score"], reverse=True)


def _candidate_from(
    row: Candidate,
    *,
    title: str,
    angle: str = "",
    content_type: str | None = None,
    buyer_stage: str | None = None,
    pillar: str | None = None,
) -> CalendarCandidate:
    return CalendarCandidate(
        canonical_topic_id=topic_family_id(row["term"]),
        title=title,
        primary_keyword=row["term"],
        intent=row["intent"],  # type: ignore[arg-type]
        pillar=pillar or row["pillar"],
        content_type=content_type,  # type: ignore[arg-type]
        buyer_stage=buyer_stage,  # type: ignore[arg-type]
        angle=angle,
        priority_score=row["score"],
        dedupe_family=topic_family_id(row["term"]),
        evidence=row["evidence"][:6],
    )


def _llm_pick(
    pool: list[Candidate], existing_titles: list[str], trends: list[TrendSignal]
) -> ContentStrategyDecisionDraft:
    payload = {
        "candidates": [
            {
                "primary_keyword": r["term"],
                "intent": r["intent"],
                "pillar": r["pillar"],
                "score": r["score"],
                "evidence": r["evidence"][:4],
            }
            for r in pool
        ],
        "recent_trends": [{"title": t.title, "points": t.points} for t in trends[:10]],
        "existing_article_titles_sample": existing_titles[:150],
        "pillars": load_niche_config().pillar_names(),
    }
    return structured(
        system_prompt=load_prompt(PROMPT, VERSION),
        user_content=inject_niche_context(dumps(payload), include_voice=False, include_keywords=True),
        schema=ContentStrategyDecisionDraft,
        tier="default",
        max_tokens=3000,
    )


def run(
    *,
    candidates: list[Candidate],
    existing_titles: list[str],
    trends: list[TrendSignal] | None = None,
    clusters: list[dict[str, Any]] | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Return {decision, calendar_candidate?, rationale, rejected, ranked}."""
    trends = trends or []
    kept, rejected = filter_candidates(candidates, existing_titles)
    ranked = rank(kept, trends, clusters or [])
    if not ranked:
        return {"decision": "skip", "skip_reason": "no candidates survived dedupe", "rejected": rejected, "ranked": []}

    pool = ranked[:TOP_N_FOR_LLM]
    by_keyword = {_normalize(r["term"]): r for r in pool}
    chosen: CalendarCandidate | None = None
    rationale = ""
    if use_llm:
        try:
            pick = _llm_pick(pool, existing_titles, trends)
        except Exception as exc:
            logger.warning("strategy LLM pick failed; using deterministic top candidate: %s", exc)
            pick = None
        if pick is not None and pick.decision == "skip":
            return {
                "decision": "skip",
                "skip_reason": pick.skip_reason or "llm_skip",
                "rejected": rejected,
                "ranked": ranked,
            }
        if pick is not None:
            row = by_keyword.get(_normalize(pick.primary_keyword))
            title_ok = natural_editorial_title(pick.title, pick.primary_keyword) and not find_duplicate(
                pick.title, existing_titles
            )
            if row is None:
                logger.warning("strategy pick %r is not in the candidate pool; falling back", pick.primary_keyword)
            elif not title_ok:
                logger.warning("strategy title %r failed quality/dedupe; using fallback title", pick.title)
                chosen = _candidate_from(
                    row,
                    title=fallback_title(row["term"]),
                    angle=pick.angle,
                    content_type=pick.content_type,
                    buyer_stage=pick.buyer_stage,
                    pillar=pick.pillar,
                )
            else:
                chosen = _candidate_from(
                    row,
                    title=" ".join(pick.title.split()),
                    angle=pick.angle,
                    content_type=pick.content_type,
                    buyer_stage=pick.buyer_stage,
                    pillar=pick.pillar,
                )
            rationale = pick.rationale
    if chosen is None:
        top = pool[0]
        chosen = _candidate_from(top, title=fallback_title(top["term"]))
        rationale = rationale or "deterministic top-ranked candidate"
    return {
        "decision": "proceed",
        "calendar_candidate": chosen.model_dump(mode="json"),
        "rationale": rationale,
        "rejected": rejected,
        "ranked": ranked,
    }
