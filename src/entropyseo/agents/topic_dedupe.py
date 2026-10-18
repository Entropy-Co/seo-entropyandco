"""Topic tokenization and duplicate-family checks.

entropyand.co already has well over a hundred articles, so the main failure mode
is re-commissioning an existing topic under a new title. Topics are reduced to a
canonical token set (aliases folded, filler dropped) and compared by overlap.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

_TOKEN_ALIASES = {
    "agents": "agent",
    "agentic": "agent",
    "automations": "automation",
    "automate": "automation",
    "automated": "automation",
    "automating": "automation",
    "workflows": "workflow",
    "llms": "llm",
    "chatbots": "chatbot",
    "bots": "chatbot",
    "bot": "chatbot",
    "agencies": "agency",
    "businesses": "business",
    "companies": "business",
    "company": "business",
    "smes": "smb",
    "sme": "smb",
    "smbs": "smb",
    "leads": "lead",
    "customers": "customer",
    "costs": "cost",
    "pricing": "cost",
    "price": "cost",
    "prices": "cost",
    "tools": "tool",
    "platforms": "platform",
    "integrations": "integration",
    "integrate": "integration",
    "crms": "crm",
    "emails": "email",
    "ads": "advertising",
    "marketing": "marketing",
    "support": "support",
    "tickets": "ticket",
    "metrics": "metric",
    "kpis": "metric",
    "kpi": "metric",
    "roi": "roi",
    "returns": "roi",
    "savings": "roi",
    "ai": "ai",
    "artificial": "ai",
    "intelligence": "ai",
    "vs": "versus",
    "compared": "versus",
    "comparison": "versus",
}
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "best",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "guide",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "our",
    "should",
    "that",
    "the",
    "their",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "why",
    "with",
    "without",
    "you",
    "your",
    "2024",
    "2025",
    "2026",
    "2027",
    "complete",
    "ultimate",
    "explained",
    "practical",
    "simple",
    "step",
    "steps",
    "ways",
    "tips",
}
# Tokens too generic on this site to distinguish two articles by themselves.
_WEAK_TOKENS = {"ai", "business", "agent", "automation", "tool", "smb", "agency"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def topic_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", _ascii(text).lower())
    tokens = {_TOKEN_ALIASES.get(word, word) for word in words}
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def topic_core_tokens(text: str) -> set[str]:
    """Distinguishing tokens: canonical tokens minus site-wide generic ones."""
    tokens = topic_tokens(text)
    core = tokens - _WEAK_TOKENS
    return core or tokens


def topic_family_id(text: str) -> str:
    """Stable id for a topic family, independent of word order and filler."""
    core = sorted(topic_core_tokens(text))
    return "-".join(core[:6]) or _SLUG_RE.sub("-", text.lower()).strip("-")[:60]


def overlap_ratio(left: str, right: str) -> float:
    """Share of the smaller topic's core tokens that the other topic also contains."""
    a, b = topic_core_tokens(left), topic_core_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def jaccard(left: str, right: str) -> float:
    a, b = topic_core_tokens(left), topic_core_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_subject(title: str) -> str:
    """The subject of a "Subject: payoff" headline, which is how this site titles nearly every post."""
    return title.split(":", 1)[0]


def find_duplicate(
    candidate: str,
    existing: Iterable[str],
    *,
    subject_threshold: float = 0.75,
    title_threshold: float = 0.6,
) -> str | None:
    """Return the first existing title that covers the candidate topic, else None.

    Containment is deliberately not enough: once generic words are dropped a
    candidate can shrink to one or two tokens that appear in dozens of unrelated
    headlines ("marketing automation for small business" is not "marketing
    attribution for small business"). A duplicate must nearly equal an existing
    headline's subject, or broadly match the whole headline.
    """
    for title in existing:
        if not title.strip():
            continue
        if topic_family_id(title) == topic_family_id(candidate):
            return title
        if jaccard(candidate, title_subject(title)) >= subject_threshold:
            return title
        if jaccard(candidate, title) >= title_threshold:
            return title
    return None


def related_titles(candidate: str, existing: Iterable[tuple[str, str]], *, limit: int = 3) -> list[tuple[str, str]]:
    """Rank (title, url) pairs by topical relatedness for internal linking. Excludes near-duplicates."""
    scored: list[tuple[float, str, str]] = []
    for title, url in existing:
        score = jaccard(candidate, title)
        if 0.15 <= score < 0.6:
            scored.append((score, title, url))
    scored.sort(key=lambda row: row[0], reverse=True)
    return [(title, url) for _, title, url in scored[:limit]]
