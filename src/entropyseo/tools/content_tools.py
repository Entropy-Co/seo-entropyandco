"""Deterministic content validators: humanization filter, entity check, article format contract.

These are not LLM calls. They gate writer output before the expert panel sees it,
and their measured actual-versus-required values become the writer's retry feedback.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from entropyseo.agents.title_quality import natural_editorial_title
from entropyseo.config import load_niche_config
from entropyseo.feeds import FeedKey, policy_for_feed

_DASHES = re.compile("[\u2014\u2013]")  # em + en dash
_AI_TELLS = [
    r"\bdive into\b",
    r"\bdelve(?:s|d)? into\b",
    r"\bin conclusion\b",
    r"\bin today's (?:fast-paced|digital|competitive)\b",
    r"\bunlock(?:s|ing)? the (?:power|potential)\b",
    r"\bharness(?:ing)? the power\b",
    r"\bembark on\b",
    r"\bit's (?:important|worth) (?:to note|noting)\b",
    r"\bleverag(?:e|es|ed|ing)\b",
    r"\bgame[-\s]chang(?:er|ing)\b",
    r"\brevolutioniz(?:e|es|ing)\b",
    r"\bever[-\s]evolving\b",
    r"\bnavigat(?:e|ing) the complexities\b",
    r"\bseamless(?:ly)?\b",
    r"\bcutting[-\s]edge\b",
    r"\btapestry\b",
    r"\btestament to\b",
    r"\bsupercharg(?:e|es|ing)\b",
]
_AI_TELLS_RE = re.compile("|".join(_AI_TELLS), re.IGNORECASE)


def humanization_check(text: str) -> dict[str, Any]:
    """Pass only when the text has no dashes, AI-tell phrases, or niche-banned phrases/characters."""
    voice = load_niche_config().voice
    dashes = len(_DASHES.findall(text))
    tells = {m.group(0).lower() for m in _AI_TELLS_RE.finditer(text)}
    lowered = text.lower()
    tells |= {phrase.lower() for phrase in voice.banned_phrases if phrase.lower() in lowered}
    banned_chars = sorted({char for char in voice.banned_characters if char and char in text})
    return {
        "passed": dashes == 0 and not tells and not banned_chars,
        "em_dashes": dashes,
        "ai_tells_found": sorted(tells),
        "banned_characters_found": banned_chars,
    }


def required_entities_present(text: str, required_entities: list[str]) -> dict[str, Any]:
    lowered = text.lower()
    missing = [e for e in required_entities if e.strip() and e.lower() not in lowered]
    return {"passed": not missing, "missing": missing}


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def title_to_slug(title: str) -> str:
    return _SLUG_RE.sub("-", title.lower()).strip("-")[:80].rstrip("-")


_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_H3_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_UL_RE = re.compile(r"^\s*[-*]\s+\S+", re.MULTILINE)
_OL_RE = re.compile(r"^\s*\d+\.\s+\S+", re.MULTILINE)
_TABLE_ROW_RE = re.compile(r"^\|.+\|\s*$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
_CTA_RE = re.compile(
    r"\b(book|schedule|talk to|contact|reach out|get in touch|start|scope|map|audit|request|see how|explore)\b",
    re.IGNORECASE,
)
_FAQ_H2_RE = re.compile(r"^##\s+(Frequently asked questions|FAQs?)\s*$", re.IGNORECASE | re.MULTILINE)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")
_GENERIC_LEAD_RE = re.compile(
    r"\b(?:this (?:article|post|guide) (?:covers|explores|looks at|will)|in this (?:article|post|guide)|"
    r"read on to|let's (?:explore|take a look)|you'll learn|we'll (?:cover|explore))\b",
    re.IGNORECASE,
)
_GENERIC_ANCHORS = {"here", "click here", "read more", "learn more", "this", "link", "this article"}
_UTILITY_HEADINGS = {"frequently asked questions", "faqs", "faq", "key takeaways", "summary", "tldr", "tl;dr"}
# Long words that are normal vocabulary for this audience; excluded from the plain-language ratio.
_DOMAIN_TERMS = {
    "automation",
    "automations",
    "workflow",
    "workflows",
    "integration",
    "integrations",
    "operations",
    "operational",
    "customers",
    "customer",
    "marketing",
    "qualification",
    "escalation",
    "escalations",
    "implementation",
    "dashboard",
    "dashboards",
    "approval",
    "approvals",
    "assistant",
    "assistants",
    "management",
    "measurement",
}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.lower()).strip()


def _normalize_url(value: str) -> str:
    return value.strip().rstrip("/")


def _markdown_link_urls(body: str) -> list[str]:
    return [m.group(2) for m in _MD_LINK_RE.finditer(body)]


def _visible(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)


def _lead_report(
    body: str, *, min_words: int, max_words: int, min_sentences: int, max_sentences: int
) -> dict[str, Any]:
    lead = body.strip().split("\n\n", 1)[0].strip()
    word_count = len(lead.split())
    sentence_count = len(_SENTENCE_RE.findall(_visible(lead)))
    generic = [m.group(0).lower() for m in _GENERIC_LEAD_RE.finditer(lead)]
    is_prose = bool(lead) and not re.match(r"^(?:#|[-*]\s|\d+\.\s|\|)", lead)
    length_passed = min_words <= word_count <= max_words
    sentences_passed = min_sentences <= sentence_count <= max_sentences
    useful = is_prose and sentences_passed and not generic
    return {
        "passed": length_passed and useful,
        "word_count": word_count,
        "min_words": min_words,
        "max_words": max_words,
        "sentence_count": sentence_count,
        "min_sentences": min_sentences,
        "max_sentences": max_sentences,
        "generic_phrases": generic,
        "length_passed": length_passed,
        "useful": useful,
    }


def _section(body: str, heading_re: re.Pattern[str]) -> tuple[int, str] | None:
    match = heading_re.search(body)
    if not match:
        return None
    rest = body[match.end() :]
    next_h2 = re.search(r"^##\s+", rest, re.MULTILINE)
    return match.start(), rest[: next_h2.start()] if next_h2 else rest


def _summary_report(body: str, *, heading: str, min_bullets: int, max_bullets: int) -> dict[str, Any]:
    found = _section(body, re.compile(rf"^##\s+{re.escape(heading.strip())}\s*$", re.IGNORECASE | re.MULTILINE))
    if not found:
        return {"passed": False, "near_top": False, "bullet_count": 0, "useful": False}
    start, section = found
    words_before = len(body[:start].split())
    bullets = [
        re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).strip()
        for line in section.splitlines()
        if re.match(r"^\s*(?:[-*]|\d+\.)\s+\S+", line)
    ]
    useful = bool(bullets) and all(5 <= len(b.split()) <= 40 and b.rstrip().endswith((".", "!", "?")) for b in bullets)
    unique = len({_normalize_text(b) for b in bullets}) == len(bullets)
    near_top = words_before <= 130
    return {
        "passed": near_top and min_bullets <= len(bullets) <= max_bullets and useful and unique,
        "near_top": near_top,
        "words_before": words_before,
        "bullet_count": len(bullets),
        "useful": useful,
        "unique": unique,
    }


def _heading_hierarchy_report(body: str) -> dict[str, Any]:
    headings = [(len(marker), text.strip()) for marker, text in _HEADING_RE.findall(body)]
    seen_h2 = False
    valid = True
    for level, _ in headings:
        if level == 2:
            seen_h2 = True
        elif (level == 3 and not seen_h2) or level > 3:
            valid = False
    duplicates = sorted(t for t, n in Counter(_normalize_text(t) for _, t in headings).items() if t and n > 1)
    return {"passed": valid and not duplicates, "duplicate_headings": duplicates}


def _repetition_report(body: str) -> dict[str, Any]:
    paragraphs = []
    for block in re.split(r"\n\s*\n", body):
        stripped = block.strip()
        if not stripped or stripped.startswith(("#", "-", "*", "|")):
            continue
        normalized = _normalize_text(_visible(stripped))
        if len(normalized.split()) >= 8:
            paragraphs.append(normalized)
    duplicate_paragraphs = sorted(p for p, n in Counter(paragraphs).items() if n > 1)
    sentences = [_normalize_text(s) for s in _SENTENCE_RE.findall(body) if len(s.split()) >= 8]
    repeated_sentences = sorted(s for s, n in Counter(sentences).items() if n >= 3)
    return {
        "passed": not duplicate_paragraphs and not repeated_sentences,
        "duplicate_paragraphs": duplicate_paragraphs,
        "repeated_sentences": repeated_sentences,
    }


def _faq_report(body: str) -> dict[str, Any]:
    found = _section(body, _FAQ_H2_RE)
    if not found:
        return {"passed": False, "question_count": 0, "invalid_answers": [], "is_last_h2": False}
    start, faq_body = found
    questions = list(_H3_RE.finditer(faq_body))
    invalid: list[str] = []
    answers: list[str] = []
    for index, match in enumerate(questions):
        end = questions[index + 1].start() if index + 1 < len(questions) else len(faq_body)
        answer = faq_body[match.end() : end].strip()
        if "?" not in match.group(1) or not 8 <= len(answer.split()) <= 90:
            invalid.append(match.group(1).strip())
        answers.append(_normalize_text(answer))
    faq_heading = _FAQ_H2_RE.search(body, start)
    after_heading = body[faq_heading.end() :] if faq_heading else ""
    is_last_h2 = re.search(r"^##\s+", after_heading, re.MULTILINE) is None
    return {
        "passed": bool(questions) and not invalid and len(set(answers)) == len(answers),
        "question_count": len(questions),
        "invalid_answers": invalid,
        "duplicate_answers": len(set(answers)) != len(answers),
        "is_last_h2": is_last_h2,
    }


def _link_contract_report(body: str, *, allowed_links: list[dict[str, Any]]) -> dict[str, Any]:
    body_urls = {_normalize_url(url) for url in _markdown_link_urls(body)}
    allowed = {_normalize_url(str(link.get("url") or "")) for link in allowed_links if link.get("url")}
    generic = sorted(
        {m.group(1).strip() for m in _MD_LINK_RE.finditer(body) if _normalize_text(m.group(1)) in _GENERIC_ANCHORS}
    )
    undeclared = sorted(body_urls - allowed)
    return {
        "passed": not undeclared and not generic,
        "body_urls": sorted(body_urls),
        "undeclared_urls": undeclared,
        "generic_anchors": generic,
    }


def _question_h2_ratio(body: str) -> float:
    h2s = [h.strip() for h in _H2_RE.findall(body) if h.strip().lower() not in _UTILITY_HEADINGS]
    if not h2s:
        return 0.0
    return round(sum(1 for h in h2s if h.endswith("?")) / len(h2s), 2)


def _keyword_count(body: str, primary_keyword: str) -> int:
    kw = re.escape(_normalize_text(primary_keyword))
    return len(re.findall(rf"(?<!\w){kw}(?!\w)", _normalize_text(body)))


def _term_coverage(haystack: str, needle: str) -> float:
    terms = {w for w in re.findall(r"[a-z0-9]+", needle.lower()) if len(w) > 2}
    if not terms:
        return 1.0
    return len(terms & set(re.findall(r"[a-z0-9]+", haystack.lower()))) / len(terms)


def _missing_required_sections(body: str, required_sections: list[str]) -> list[str]:
    headings = " ".join([*_H2_RE.findall(body), *_H3_RE.findall(body)])
    return [s for s in (x.strip() for x in required_sections) if s and _term_coverage(headings, s) < 0.5]


def _has_cta(body: str, cta_url: str) -> bool:
    """A CTA is a link to the brief's CTA URL with action wording in the closing section before the FAQ."""
    if not cta_url:
        return False
    faq = _FAQ_H2_RE.search(body)
    editorial = body[: faq.start()] if faq else body
    closing = editorial[-1800:]
    target = _normalize_url(cta_url)
    linked = any(_normalize_url(m.group(2)) == target for m in _MD_LINK_RE.finditer(closing))
    return linked and bool(_CTA_RE.search(closing))


def _plain_language_report(body: str, *, max_avg: float, max_long_ratio: float) -> dict[str, Any]:
    text = _visible(body)
    sentences = [s for s in _SENTENCE_RE.findall(text) if s.strip()]
    words = _WORD_RE.findall(text)
    avg = round(len(words) / max(1, len(sentences)), 1)
    long_words = [w for w in words if len(w) >= 10 and w.lower() not in _DOMAIN_TERMS and not w.isupper()]
    ratio = round(len(long_words) / max(1, len(words)), 3)
    return {
        "average_sentence_words": avg,
        "long_word_ratio": ratio,
        "max_average_sentence_words": max_avg,
        "max_long_word_ratio": max_long_ratio,
        "passed": avg <= max_avg and ratio <= max_long_ratio,
    }


def blog_article_format_check(
    *,
    title: str,
    meta_title: str,
    meta_description: str,
    body_markdown: str,
    primary_keyword: str,
    allowed_links: list[dict[str, Any]],
    min_words: int,
    max_words: int,
    min_faq_count: int,
    max_faq_count: int | None,
    summary_heading: str,
    cta_url: str = "",
    cta_required: bool = True,
    required_sections: list[str] | None = None,
    feed: FeedKey = "insights",
) -> dict[str, Any]:
    """Validate the article contract. Returns blocking `failed`, advisory `advisory_failed`, and measured values."""
    policy = policy_for_feed(feed)
    body = body_markdown
    normalized_keyword = _normalize_text(primary_keyword)
    h2s = _H2_RE.findall(body)
    h3s = _H3_RE.findall(body)
    body_urls = {_normalize_url(u) for u in _markdown_link_urls(body)}
    internal_allowed = [
        _normalize_url(str(link["url"])) for link in allowed_links if link.get("kind") == "internal" and link.get("url")
    ]
    external_allowed = [
        _normalize_url(str(link["url"])) for link in allowed_links if link.get("kind") == "external" and link.get("url")
    ]
    internal_used = [u for u in internal_allowed if u in body_urls]
    external_used = [u for u in external_allowed if u in body_urls]
    # Internal links: at least two when the brief offers two (service page + related article).
    internal_needed = min(2, len(internal_allowed))
    word_count = len(body.split())
    keyword_uses = _keyword_count(body, primary_keyword)
    intro = " ".join(body.split()[:120])
    lead = _lead_report(
        body,
        min_words=policy.min_lead_words,
        max_words=policy.max_lead_words,
        min_sentences=policy.min_lead_sentences,
        max_sentences=policy.max_lead_sentences,
    )
    summary = _summary_report(
        body, heading=summary_heading, min_bullets=policy.min_summary_bullets, max_bullets=policy.max_summary_bullets
    )
    hierarchy = _heading_hierarchy_report(body)
    repetition = _repetition_report(body)
    faq = _faq_report(body)
    links = _link_contract_report(body, allowed_links=allowed_links)
    plain = _plain_language_report(
        body, max_avg=policy.max_avg_sentence_words, max_long_ratio=policy.max_long_word_ratio
    )
    question_ratio = _question_h2_ratio(body)
    missing_sections = _missing_required_sections(body, required_sections or [])
    faq_count_ok = faq["question_count"] >= min_faq_count and (
        max_faq_count is None or faq["question_count"] <= max_faq_count
    )

    checks: dict[str, bool] = {
        "title_matches_keyword_intent": natural_editorial_title(title, primary_keyword),
        "body_has_no_h1": not _H1_RE.search(body),
        "meta_title_length": 10 <= len(meta_title.strip()) <= 60,
        "meta_description_length": 80 <= len(meta_description.strip()) <= 160,
        "meta_description_contains_keyword": bool(normalized_keyword)
        and normalized_keyword in _normalize_text(meta_description),
        "meta_description_complete": meta_description.strip().endswith((".", "!", "?")),
        "word_count_range": min_words <= word_count <= max_words,
        "has_h2": len(h2s) >= policy.min_h2_count,
        "has_h3": len(h3s) >= 1,
        "heading_hierarchy": bool(hierarchy["passed"]),
        "keyword_in_intro": bool(normalized_keyword) and normalized_keyword in _normalize_text(intro),
        "keyword_in_h2": any(normalized_keyword in _normalize_text(h) for h in h2s),
        "keyword_not_stuffed": policy.min_primary_keyword_uses <= keyword_uses <= policy.max_primary_keyword_uses,
        "has_scannable_structure": bool(_UL_RE.search(body) or _OL_RE.search(body) or _TABLE_ROW_RE.search(body)),
        "summary_block_near_top": bool(summary["passed"]),
        "lead_is_concise": bool(lead["length_passed"]),
        "lead_is_useful": bool(lead["useful"]),
        "question_h2_ratio": question_ratio >= policy.min_question_h2_ratio,
        "has_faqs": faq_count_ok and bool(faq["passed"]) and bool(faq["is_last_h2"]),
        "has_internal_links": len(internal_used) >= internal_needed,
        "has_external_links": bool(external_used) or not external_allowed,
        "has_cta": _has_cta(body, cta_url) or not cta_required,
        "links_match_brief": bool(links["passed"]),
        "content_is_not_repetitive": bool(repetition["passed"]),
        "plain_language": bool(plain["passed"]),
        "required_sections_covered": not missing_sections or len(missing_sections) <= len(required_sections or []) // 3,
    }
    all_failed = [name for name, passed in checks.items() if not passed]
    advisory = [name for name in all_failed if name in policy.advisory_format_checks]
    failed = [name for name in all_failed if name not in policy.advisory_format_checks]
    return {
        "passed": not failed,
        "failed": failed,
        "advisory_failed": advisory,
        "checks": checks,
        "word_count": word_count,
        "keyword_uses": keyword_uses,
        "h2_count": len(h2s),
        "h3_count": len(h3s),
        "question_h2_ratio": question_ratio,
        "meta_title_length": len(meta_title.strip()),
        "meta_description_length": len(meta_description.strip()),
        "internal_links_used": internal_used,
        "internal_links_needed": internal_needed,
        "external_links_used": external_used,
        "targets": {
            "word_count": {"minimum": min_words, "maximum": max_words},
            "keyword_uses": {"minimum": policy.min_primary_keyword_uses, "maximum": policy.max_primary_keyword_uses},
            "question_h2_ratio": {"minimum": policy.min_question_h2_ratio},
            "faq": {"minimum": min_faq_count, "maximum": max_faq_count},
        },
        "lead": lead,
        "summary": summary,
        "heading_hierarchy": hierarchy,
        "repetition": repetition,
        "faq": faq,
        "link_contract": links,
        "plain_language": plain,
        "missing_required_sections": missing_sections,
    }
