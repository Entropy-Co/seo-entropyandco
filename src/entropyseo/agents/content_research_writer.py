"""content-research-writer agent: turn a calendar topic into a ContentBrief.

The brief is the writer's only factual and link contract:
- source_context: fetched text from approved authority pages and Entropy's own service pages
- verified_facts / must_include_stats: LLM-proposed, then kept only when the cited
  source's fetched text actually contains them
- links: an allowlist (service page, related existing articles, contact CTA,
  fetched authority sources). The format checker rejects any other URL.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from entropyseo.agents.topic_dedupe import related_titles
from entropyseo.config import get_settings, load_niche_config
from entropyseo.feeds import FeedKey, policy_for_feed
from entropyseo.llm import dumps, inject_niche_context, load_prompt, structured
from entropyseo.schemas import (
    BriefLink,
    BriefStat,
    CalendarCandidate,
    ContentBrief,
    ContentBriefDraft,
    PublishedPost,
    SourceContext,
    VerifiedFact,
)
from entropyseo.tools import search_tools, web_tools

logger = logging.getLogger(__name__)

PROMPT = "content-research-writer"
VERSION = "v1.0"
MAX_AUTHORITY_SOURCES = 5
MIN_EXCERPT_WORDS = 40
_NUMBER_RE = re.compile(r"\d[\d,.]*%?")
_WORD_RE = re.compile(r"[a-z]{5,}")


def _source_type(url: str) -> str:
    host = web_tools.host_of(url)
    if host.endswith(get_settings().site_host()):
        return "first_party"
    if any(marker in host for marker in ("docs.", "developers.", "developer.", "support.", "help.")):
        return "official_docs"
    if host.endswith((".gov", ".edu")) or host.endswith(("nist.gov", "oecd.org", "europa.eu")):
        return "standards"
    if any(
        marker in host
        for marker in ("mckinsey", "gartner", "forrester", "deloitte", "bcg", "hbr", "stanford", "mit.edu")
    ):
        return "research"
    return "authority"


def _approved_url(url: str) -> bool:
    niche = load_niche_config()
    if not url.startswith("https://"):
        return False
    if web_tools.host_matches(url, [get_settings().site_host()]):
        return False
    if niche.competitor_domains and web_tools.host_matches(url, niche.competitor_domains):
        return False
    return web_tools.host_matches(url, niche.approved_external_domains)


def authority_source_urls(candidate: CalendarCandidate) -> list[str]:
    """SERP results on approved domains first, then the pillar's configured seed sources."""
    niche = load_niche_config()
    urls: list[str] = []
    for query in (candidate.primary_keyword, candidate.title):
        for result in search_tools.serpapi_expand(query)["top_results"]:
            if _approved_url(result["url"]):
                urls.append(result["url"])
        if len(urls) >= MAX_AUTHORITY_SOURCES:
            break
    pillar_seeds = niche.seed_sources.get(candidate.pillar or "", [])
    urls.extend(pillar_seeds)
    return list(dict.fromkeys(urls))[: MAX_AUTHORITY_SOURCES + 2]


def fetch_sources(urls: list[str]) -> list[SourceContext]:
    sources: list[SourceContext] = []
    for url in urls:
        title, excerpt = web_tools.fetch_source_excerpt(url)
        if len(excerpt.split()) < MIN_EXCERPT_WORDS:
            logger.info("research source unreadable url=%s", url)
            continue
        sources.append(SourceContext(title=title, url=url, source_type=_source_type(url), excerpt=excerpt))  # type: ignore[arg-type]
        if sum(1 for s in sources if s.source_type != "first_party") >= MAX_AUTHORITY_SOURCES:
            break
    return sources


def internal_links(candidate: CalendarCandidate, corpus: list[PublishedPost]) -> tuple[list[BriefLink], str]:
    """Service page for the pillar, up to three related existing articles, and the CTA URL."""
    niche = load_niche_config()
    s = get_settings()
    links: list[BriefLink] = []
    pillar = niche.pillar(candidate.pillar)
    service_pages = [p for p in niche.internal_pages if candidate.pillar and candidate.pillar in p.pillars]
    if pillar and pillar.service_url and not any(p.url == pillar.service_url for p in service_pages):
        links.append(BriefLink(anchor=pillar.name, url=pillar.service_url, kind="internal"))
    links.extend(BriefLink(anchor=p.anchor, url=p.url, kind="internal") for p in service_pages[:2])
    related = related_titles(candidate.title, [(p.title, p.url) for p in corpus if p.title and p.url])
    links.extend(BriefLink(anchor=title, url=url, kind="internal") for title, url in related)
    cta_page = next((p for p in niche.internal_pages if "cta" in p.pillars), None)
    cta_url = cta_page.url if cta_page else f"{s.site_url.rstrip('/')}/contact"
    if not any(link.url == cta_url for link in links):
        links.append(
            BriefLink(anchor=cta_page.anchor if cta_page else "talk to our team", url=cta_url, kind="internal")
        )
    return list({link.url: link for link in links}.values()), cta_url


def first_party_sources(links: list[BriefLink]) -> list[SourceContext]:
    """Fetch Entropy's own service pages so offer descriptions come from the site, not the model."""
    service_urls = [link.url for link in links if "/services/" in link.url][:2]
    return [s for s in fetch_sources(service_urls) if s.source_type == "first_party"]


def _supported(text: str, excerpt: str) -> bool:
    """A claim is supported when its numbers all appear in the excerpt and most of its content words do."""
    lowered = excerpt.lower()
    numbers = _NUMBER_RE.findall(text)
    if any(n.rstrip(".,") not in lowered for n in numbers):
        return False
    words = set(_WORD_RE.findall(text.lower()))
    if not words:
        return bool(numbers)
    return len({w for w in words if w in lowered}) / len(words) >= 0.5


def verify_stats(stats: list[BriefStat], sources: list[SourceContext]) -> list[BriefStat]:
    by_url = {s.url.rstrip("/"): s for s in sources}
    kept = []
    for stat in stats:
        source = by_url.get(stat.source.rstrip("/"))
        if source and _supported(f"{stat.stat} {stat.value}", source.excerpt):
            kept.append(stat)
    return kept


def verify_facts(facts: list[VerifiedFact], sources: list[SourceContext]) -> list[VerifiedFact]:
    by_url = {s.url.rstrip("/"): s for s in sources}
    kept = []
    for fact in facts:
        source = by_url.get(fact.source_url.rstrip("/"))
        if source and _supported(fact.fact, source.excerpt):
            kept.append(fact.model_copy(update={"source_title": fact.source_title or source.title}))
    return kept


def _anchor_for(source: SourceContext) -> str:
    title = re.split(r"\s[|\-:]\s", source.title)[0].strip() if source.title else ""
    return title or web_tools.host_of(source.url)


def run(
    candidate: CalendarCandidate,
    *,
    corpus: list[PublishedPost],
    feed: FeedKey = "insights",
    run_date: str | None = None,
) -> ContentBrief:
    policy = policy_for_feed(feed)
    run_date = run_date or datetime.now(UTC).date().isoformat()
    links, cta_url = internal_links(candidate, corpus)
    authority = fetch_sources(authority_source_urls(candidate))
    first_party = first_party_sources(links)
    sources = [*first_party, *authority]
    sources_unavailable = not authority
    if sources_unavailable:
        logger.warning("no authority source could be fetched topic=%s", candidate.title)

    payload: dict[str, Any] = {
        "date": run_date,
        "topic": candidate.title,
        "primary_keyword": candidate.primary_keyword,
        "pillar": candidate.pillar,
        "content_type": candidate.content_type,
        "buyer_stage": candidate.buyer_stage,
        "angle_hint": candidate.angle,
        "selection_evidence": candidate.evidence,
        "word_range": [policy.min_words, policy.max_words],
        "sources": [s.model_dump(mode="json") for s in sources],
        "internal_links": [link.model_dump(mode="json") for link in links],
        "serp_top_results": search_tools.serpapi_expand(candidate.primary_keyword)["top_results"][:8],
    }
    try:
        draft = structured(
            system_prompt=load_prompt(PROMPT, VERSION),
            user_content=inject_niche_context(dumps(payload), include_voice=False),
            schema=ContentBriefDraft,
            tier="default",
            max_tokens=5000,
        )
    except Exception as exc:
        logger.warning("brief generation failed; using a minimal brief: %s", exc)
        draft = ContentBriefDraft()

    stats = verify_stats(draft.must_include_stats, sources)
    facts = verify_facts(draft.verified_facts, sources)
    dropped = (len(draft.must_include_stats) - len(stats)) + (len(draft.verified_facts) - len(facts))
    if dropped:
        logger.info("dropped %s unsupported stats/facts from the brief", dropped)

    approved_external = {s.url for s in authority}
    chosen_external = [u for u in draft.external_link_urls if u in approved_external] or [s.url for s in authority]
    external_links = [
        BriefLink(anchor=_anchor_for(s), url=s.url, kind="external") for s in authority if s.url in chosen_external
    ]
    source_policy = (
        "Concrete numbers, prices, benchmarks, tool capabilities, dates, and company claims require a verified_fact, "
        "must_include_stat, or source_context excerpt that states them. A source title or URL alone is not evidence. "
        "Omit unsupported claims instead of hedging them. Worked examples and cost math are allowed when every input "
        "is labeled as an assumption the reader should replace with their own numbers."
    )
    if sources_unavailable:
        source_policy += (
            " No external authority page could be fetched for this brief, so write from verified facts, Entropy's own "
            "service pages, and clearly framed operator guidance. Keep external statistics out entirely."
        )
    return ContentBrief(
        topic=candidate.title,
        primary_keyword=candidate.primary_keyword,
        feed=feed,
        calendar_candidate=candidate,
        run_date=run_date,
        angle=draft.angle or candidate.angle,
        target_reader=draft.target_reader,
        target_length_words=policy.target_length_words,
        min_words=policy.min_words,
        max_words=policy.max_words,
        min_faq_count=policy.min_faq_count,
        max_faq_count=policy.max_faq_count,
        summary_heading=policy.summary_heading,
        h2_structure=draft.h2_structure,
        must_include_entities=draft.must_include_entities[:8],
        must_include_stats=stats,
        links=[*links, *external_links],
        verified_facts=facts,
        source_context=sources,
        cta_url=cta_url,
        cta_required=True,
        source_policy=source_policy,
        evidence_floor_enforced=not sources_unavailable,
        serp_gap=draft.serp_gap,
        research_sources=[s.url for s in sources],
        required_sections=draft.required_sections[:6],
    )
