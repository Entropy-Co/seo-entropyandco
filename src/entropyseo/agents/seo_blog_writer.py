"""seo-blog-writer agent: generate a draft, validate it deterministically, revise in place.

Loop per call: generate -> humanization + entity + format checks -> if anything
fails, hand the writer its own previous draft plus measured actual-versus-required
feedback and revise (never restart). The best complete draft is kept; when advisory
misses stop shrinking, further rewrites only chase the same targets, so we stop.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from entropyseo.config import get_settings
from entropyseo.feeds import policy_for_feed
from entropyseo.llm import dumps, inject_niche_context, load_prompt, structured
from entropyseo.runtime_logging import log_event
from entropyseo.schemas import BlogArticleDraft, BlogDraftContent, BriefLink, ContentBrief
from entropyseo.tools.content_tools import (
    blog_article_format_check,
    humanization_check,
    required_entities_present,
    title_to_slug,
)

PROMPT = "seo-blog-writer"
VERSION = "v1.0"
logger = logging.getLogger(__name__)

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")


def _invoke(brief_payload: dict[str, Any], extra: str, *, revision: bool) -> BlogDraftContent:
    user = f"brief:\n{dumps(brief_payload)}"
    if extra:
        user += f"\n\n{extra}"
    return structured(
        system_prompt=load_prompt(PROMPT, VERSION),
        user_content=inject_niche_context(user),
        schema=BlogDraftContent,
        tier="heavy",
        max_tokens=get_settings().blog_writer_max_tokens,
        # Fresh drafts get full depth; revisions are targeted edits to an existing article.
        effort="medium" if revision else None,
    )


def _title(brief: ContentBrief) -> str:
    return " ".join((brief.calendar_candidate.title if brief.calendar_candidate else brief.topic).split())


def _writer_payload(brief: ContentBrief, title: str) -> dict[str, Any]:
    policy = policy_for_feed(brief.feed)
    payload = brief.model_dump(mode="json", exclude={"calendar_candidate"})
    payload["topic"] = title
    payload["format_targets"] = {
        "word_count": {"target": brief.target_length_words, "minimum": brief.min_words, "maximum": brief.max_words},
        "lead": {
            "minimum_words": policy.min_lead_words,
            "maximum_words": policy.max_lead_words,
            "minimum_sentences": policy.min_lead_sentences,
            "maximum_sentences": policy.max_lead_sentences,
        },
        "primary_keyword": {
            "exact_phrase": brief.primary_keyword,
            "minimum_uses": policy.min_primary_keyword_uses,
            "maximum_uses": policy.max_primary_keyword_uses,
            "required_in_h2": True,
        },
        "minimum_question_h2_ratio": policy.min_question_h2_ratio,
        "summary": {
            "heading": brief.summary_heading,
            "bullets": [policy.min_summary_bullets, policy.max_summary_bullets],
        },
        "faq": {"minimum": brief.min_faq_count, "maximum": brief.max_faq_count},
        "internal_links_minimum": min(2, sum(1 for link in brief.links if link.kind == "internal")),
        "cta_url": brief.cta_url,
    }
    return payload


def _evidence_preflight(brief: ContentBrief) -> dict[str, Any]:
    fragments = {
        " ".join(v.split())
        for v in [
            *[s.excerpt for s in brief.source_context],
            *[f.fact for f in brief.verified_facts],
            *[f"{s.stat} {s.value}" for s in brief.must_include_stats],
        ]
        if v.strip()
    }
    words = sum(len(f.split()) for f in fragments)
    minimum = policy_for_feed(brief.feed).min_evidence_words
    return {
        "passed": not brief.evidence_floor_enforced or words >= minimum,
        "enforced": brief.evidence_floor_enforced,
        "evidence_words": words,
        "minimum_evidence_words": minimum,
        "source_count": len(brief.source_context),
        "verified_fact_count": len(brief.verified_facts),
    }


def _links_used(brief: ContentBrief, body: str) -> tuple[list[BriefLink], list[BriefLink]]:
    body_urls = {m.group(1).strip().rstrip("/") for m in _MARKDOWN_LINK_RE.finditer(body)}
    used = [link for link in brief.links if link.url.rstrip("/") in body_urls]
    return [link for link in used if link.kind == "internal"], [link for link in used if link.kind == "external"]


def format_check(brief: ContentBrief, *, title: str, content: BlogDraftContent) -> dict[str, Any]:
    return blog_article_format_check(
        title=title,
        meta_title=content.meta_title,
        meta_description=content.meta_description,
        body_markdown=content.body_markdown.strip(),
        primary_keyword=brief.primary_keyword,
        allowed_links=[link.model_dump(mode="json") for link in brief.links],
        min_words=brief.min_words,
        max_words=brief.max_words,
        min_faq_count=brief.min_faq_count,
        max_faq_count=brief.max_faq_count,
        summary_heading=brief.summary_heading,
        cta_url=brief.cta_url,
        cta_required=brief.cta_required,
        required_sections=brief.required_sections,
        feed=brief.feed,
    )


def _names(report: dict[str, Any], key: str) -> list[str]:
    value = report.get(key)
    return [str(v) for v in value] if isinstance(value, list) else []


def _approved(brief: ContentBrief, kind: str) -> str:
    return ", ".join(f'"{link.anchor}" ({link.url})' for link in brief.links if link.kind == kind)


def format_feedback(report: dict[str, Any], brief: ContentBrief) -> str:
    """Name each miss with its measured value so the retry fixes it instead of guessing."""
    blocking, advisory = _names(report, "failed"), _names(report, "advisory_failed")
    misses = {*blocking, *advisory}
    out: list[str] = []
    if blocking:
        out.append("Blocking contract failures: " + ", ".join(blocking) + ".")
    if advisory:
        out.append("Advisory targets missed: " + ", ".join(advisory) + ".")
    if "word_count_range" in misses:
        out.append(f"Body is {report['word_count']:,} words; required {brief.min_words:,}-{brief.max_words:,}.")
    if "keyword_in_h2" in misses:
        out.append(f'Use the exact primary keyword "{brief.primary_keyword}" naturally in one H2.')
    if "keyword_in_intro" in misses:
        out.append(f'Use the exact primary keyword "{brief.primary_keyword}" in the first 120 words.')
    if "keyword_not_stuffed" in misses:
        t = report["targets"]["keyword_uses"]
        out.append(f"Primary keyword has {report['keyword_uses']} exact uses; required {t['minimum']}-{t['maximum']}.")
    lead = report.get("lead") or {}
    if {"lead_is_concise", "lead_is_useful"} & misses:
        out.append(
            f"Rewrite the opening paragraph as a direct answer: it is {lead.get('word_count')} words and "
            f"{lead.get('sentence_count')} sentences; required {lead.get('min_words')}-{lead.get('max_words')} words and "
            f"{lead.get('min_sentences')}-{lead.get('max_sentences')} sentences."
        )
        if lead.get("generic_phrases"):
            out.append("Remove generic framing: " + ", ".join(lead["generic_phrases"]) + ".")
    if "summary_block_near_top" in misses:
        s = report.get("summary") or {}
        out.append(
            f'Put "## {brief.summary_heading}" right after the opening paragraph with 3-5 complete-sentence bullets '
            f"(found {s.get('bullet_count', 0)} bullets, {s.get('words_before', '?')} words before it)."
        )
    if "question_h2_ratio" in misses:
        out.append(
            f"{report['question_h2_ratio']:.0%} of non-utility H2s are questions; make at least 35% genuine reader questions."
        )
    if "has_faqs" in misses:
        faq = report.get("faq") or {}
        out.append(
            f"FAQ has {faq.get('question_count', 0)} valid pairs; required {brief.min_faq_count}-{brief.max_faq_count}. "
            "Make '## Frequently asked questions' the final H2, each pair an H3 question with an 8-90 word answer."
        )
        if faq.get("invalid_answers"):
            out.append("Fix these FAQ entries: " + " | ".join(faq["invalid_answers"][:5]))
    if "has_internal_links" in misses:
        out.append(
            f"Link at least {report['internal_links_needed']} internal pages in relevant prose: {_approved(brief, 'internal')}."
        )
    if "has_external_links" in misses:
        out.append(
            "Cite at least one approved external source with a natural anchor: " + _approved(brief, "external") + "."
        )
    if "has_cta" in misses:
        out.append(
            f"End the editorial body (before the FAQ) with one restrained, specific next step linking {brief.cta_url} "
            "with action wording (for example: book a workflow audit, map your first automation)."
        )
    links = report.get("link_contract") or {}
    if links.get("undeclared_urls"):
        out.append("Remove links that are not in the approved list: " + ", ".join(links["undeclared_urls"][:6]) + ".")
    if links.get("generic_anchors"):
        out.append("Replace generic anchors: " + ", ".join(links["generic_anchors"]) + ".")
    if "meta_description_length" in misses:
        out.append(
            f"meta_description is {report['meta_description_length']} characters; required 80-160 with the primary keyword."
        )
    if "meta_title_length" in misses:
        out.append(f"meta_title is {report['meta_title_length']} characters; required 10-60.")
    if "plain_language" in misses:
        p = report["plain_language"]
        out.append(
            f"Simplify: average sentence is {p['average_sentence_words']} words (max {p['max_average_sentence_words']}) "
            f"and {p['long_word_ratio']:.1%} of words are 10+ letters (max {p['max_long_word_ratio']:.0%})."
        )
    rep = report.get("repetition") or {}
    if "content_is_not_repetitive" in misses:
        repeated = [*rep.get("duplicate_paragraphs", [])[:2], *rep.get("repeated_sentences", [])[:2]]
        out.append("Remove repeated passages: " + " | ".join(r[:120] for r in repeated))
    if report.get("missing_required_sections"):
        out.append("Cover these required sections: " + ", ".join(report["missing_required_sections"]) + ".")
    if "body_has_no_h1" in misses:
        out.append("Remove the Markdown H1; the CMS title is the only H1.")
    if "title_matches_keyword_intent" in misses:
        out.append("The title is fixed by the calendar; do not change it. Keep it exactly as given.")
    return " ".join(out)


def _revision_block(content: BlogDraftContent) -> str:
    body = content.body_markdown.strip()
    if not body:
        return ""
    previous = {
        "meta_title": content.meta_title.strip(),
        "meta_description": content.meta_description.strip(),
        "excerpt": content.excerpt.strip(),
        "body_markdown": body,
    }
    return (
        "Previous draft (revise this; do not start over):\n"
        f"{dumps(previous)}\n"
        "Keep every section, heading, link, and fact that already satisfies the contract. Change only what the "
        "feedback requires, and keep the body within the required word range."
    )


def _fallback_meta_title(title: str) -> str:
    value = title[:60].rstrip()
    return value if len(value) >= 10 else "Draft review required"


def _fallback_meta_description(keyword: str) -> str:
    text = f"{keyword[:70]} draft failed validation and needs editorial review before publication."
    return (text if len(text) >= 80 else text + " No live article was created.")[:160]


def _build(
    brief: ContentBrief,
    *,
    title: str,
    slug: str,
    content: BlogDraftContent,
    report: dict[str, Any],
    passed: bool,
    humanized: bool,
    version_suffix: str = "",
) -> BlogArticleDraft:
    body = content.body_markdown.strip() or "Draft generation failed. No publishable body was returned."
    internal, external = _links_used(brief, body)
    meta_title = content.meta_title.strip()
    meta_description = content.meta_description.strip()
    words = len(body.split())
    return BlogArticleDraft(
        title=title,
        slug=slug,
        meta_title=meta_title if 10 <= len(meta_title) <= 60 else _fallback_meta_title(title),
        meta_description=meta_description
        if 80 <= len(meta_description) <= 160
        else _fallback_meta_description(brief.primary_keyword),
        excerpt=content.excerpt.strip()[:300],
        body_markdown=body,
        primary_keyword=brief.primary_keyword,
        keywords=content.keywords[:8],
        internal_links_used=internal,
        external_links_used=external,
        word_count=words,
        estimated_read_time_minutes=max(1, round(words / 230)),
        humanization_passed=humanized,
        format_passed=passed,
        format_report=report,
        prompt_version=f"{PROMPT}-{VERSION}{version_suffix}",
        generated_at=datetime.now(UTC),
    )


def _failed(
    brief: ContentBrief,
    *,
    title: str,
    slug: str,
    failure: str,
    content: BlogDraftContent | None = None,
    report: dict[str, Any] | None = None,
    humanized: bool = False,
    detail: str = "",
) -> BlogArticleDraft:
    report = dict(report or {})
    report["passed"] = False
    report["failed"] = list(dict.fromkeys([*_names(report, "failed"), failure]))
    if detail:
        report["writer_error"] = detail
    return _build(
        brief,
        title=title,
        slug=slug,
        content=content or BlogDraftContent(),
        report=report,
        passed=False,
        humanized=humanized,
        version_suffix="-FAILED",
    )


def run(
    brief: ContentBrief, revision_feedback: str | None = None, *, draft_to_revise: BlogArticleDraft | None = None
) -> BlogArticleDraft:
    title = _title(brief)
    slug = title_to_slug(title)
    evidence = _evidence_preflight(brief)
    if not evidence["passed"]:
        log_event(logger, logging.WARNING, "writer.evidence_hold", **evidence)
        return _failed(
            brief, title=title, slug=slug, failure="insufficient_source_evidence", report={"source_evidence": evidence}
        )

    max_retries = get_settings().writer_max_retries
    payload = _writer_payload(brief, title)
    panel_block = (
        "Revision requirements from the editorial panel:\n"
        + revision_feedback
        + "\nRewrite the affected prose in place."
        if revision_feedback
        else ""
    )
    previous_content = (
        BlogDraftContent(
            meta_title=draft_to_revise.meta_title,
            meta_description=draft_to_revise.meta_description,
            excerpt=draft_to_revise.excerpt,
            body_markdown=draft_to_revise.body_markdown,
        )
        if draft_to_revise is not None
        else None
    )
    fallback = draft_to_revise if draft_to_revise is not None and draft_to_revise.format_passed else None
    extra = "\n\n".join(x for x in (panel_block, _revision_block(previous_content) if previous_content else "") if x)
    try:
        content = _invoke(payload, extra, revision=previous_content is not None)
    except Exception as exc:
        logger.warning("writer generation failed: %s", exc)
        return fallback or _failed(
            brief, title=title, slug=slug, failure="writer_generation_failed", detail=type(exc).__name__
        )

    best: BlogArticleDraft | None = None
    best_advisory = previous_advisory = 10_000
    last_report: dict[str, Any] = {}
    last_humanized = False
    for attempt in range(max_retries + 1):
        body = content.body_markdown.strip()
        human = humanization_check(" ".join([title, content.meta_description, content.excerpt, body]))
        entities = required_entities_present(body, brief.must_include_entities)
        report = format_check(brief, title=title, content=content)
        last_report, last_humanized = report, bool(human["passed"])
        log_event(
            logger,
            logging.INFO,
            "writer.contract",
            attempt=f"{attempt + 1}/{max_retries + 1}",
            blocking="|".join(_names(report, "failed")) or "(none)",
            advisory="|".join(_names(report, "advisory_failed")) or "(none)",
            humanized=human["passed"],
            words=report["word_count"],
        )
        feedback: list[str] = []
        if not human["passed"]:
            feedback.append(
                f"Humanization failed: dashes={human['em_dashes']} banned/AI phrases={human['ai_tells_found']} "
                f"banned characters={human['banned_characters_found']}. Rewrite those sentences."
            )
        if not entities["passed"]:
            feedback.append(f"Missing required entities: {entities['missing']}.")
        if not report["passed"] or report["advisory_failed"]:
            feedback.append(format_feedback(report, brief))
        complete = bool(human["passed"]) and bool(entities["passed"]) and bool(report["passed"])
        if complete:
            draft = _build(brief, title=title, slug=slug, content=content, report=report, passed=True, humanized=True)
            advisory_count = len(report["advisory_failed"])
            if best is None or advisory_count < best_advisory:
                best, best_advisory = draft, advisory_count
            elif advisory_count >= previous_advisory:
                return best
            previous_advisory = advisory_count
            if not feedback:
                return draft
        if attempt < max_retries:
            try:
                content = _invoke(
                    payload,
                    "\n\n".join(x for x in (panel_block, "\n".join(feedback), _revision_block(content)) if x)
                    + "\nReturn the complete revised draft.",
                    revision=True,
                )
            except Exception as exc:
                logger.warning("writer revision failed: %s", exc)
                return (
                    best
                    or fallback
                    or _failed(
                        brief,
                        title=title,
                        slug=slug,
                        failure="writer_generation_failed",
                        content=content,
                        report=last_report,
                        humanized=last_humanized,
                        detail=type(exc).__name__,
                    )
                )
    return (
        best
        or fallback
        or _failed(
            brief,
            title=title,
            slug=slug,
            failure="writer_retries_exhausted",
            content=content,
            report=last_report,
            humanized=last_humanized,
        )
    )
