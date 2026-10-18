"""Title and keyword quality helpers for strategy and calendar rows."""

from __future__ import annotations

import re

from entropyseo.agents.topic_dedupe import topic_tokens

MAX_TITLE_CHARS = 70
MAX_KEYWORD_WORDS = 7
_LOW_FIT_TERMS_RE = re.compile(r"\b(?:near me|free download|torrent|login|jobs?|salary|course|certification)\b", re.I)
_QUESTION_PREFIX_RE = re.compile(r"^(?:how (?:do|can|to)|what (?:is|are)|why (?:do|does|is)|should i|can i)\s+", re.I)
_ACRONYMS = {
    "ai": "AI",
    "crm": "CRM",
    "llm": "LLM",
    "llms": "LLMs",
    "roi": "ROI",
    "seo": "SEO",
    "smb": "SMB",
    "smbs": "SMBs",
    "kpi": "KPI",
    "kpis": "KPIs",
    "api": "API",
    "apis": "APIs",
    "b2b": "B2B",
    "saas": "SaaS",
    "cmo": "CMO",
    "bpo": "BPO",
    "rag": "RAG",
    "n8n": "n8n",
    "hubspot": "HubSpot",
    "gohighlevel": "GoHighLevel",
}
_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "with"}


def title_case_keyword(term: str) -> str:
    words = term.split()
    out: list[str] = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered in _ACRONYMS:
            out.append(_ACRONYMS[lowered])
        elif index > 0 and lowered in _SMALL_WORDS:
            out.append(lowered)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


def search_keyword_from_phrase(phrase: str, *, max_words: int = MAX_KEYWORD_WORDS) -> str:
    """Turn a question or long phrase into a compact search keyword, preserving token order."""
    text = _QUESTION_PREFIX_RE.sub("", " ".join(phrase.lower().split())).rstrip("?").strip()
    words = text.split()
    return " ".join(words[:max_words])


def queueable_keyword(primary_keyword: str) -> bool:
    words = primary_keyword.split()
    return 2 <= len(words) <= MAX_KEYWORD_WORDS and not _LOW_FIT_TERMS_RE.search(primary_keyword)


def keyword_echo_title(title: str, primary_keyword: str) -> bool:
    """True when the title is just the keyword with title casing (a weak, templated headline)."""
    return " ".join(title.lower().split()).rstrip("?") == " ".join(primary_keyword.lower().split())


def natural_editorial_title(title: str, primary_keyword: str) -> bool:
    """A usable title: right length, covers most keyword terms, and is more than a keyword echo."""
    clean = " ".join(title.split())
    if not 20 <= len(clean) <= MAX_TITLE_CHARS:
        return False
    if keyword_echo_title(clean, primary_keyword):
        return False
    keyword_terms = topic_tokens(primary_keyword)
    if not keyword_terms:
        return True
    return len(keyword_terms & topic_tokens(clean)) / len(keyword_terms) >= 0.6


def fallback_title(primary_keyword: str) -> str:
    base = title_case_keyword(primary_keyword)
    title = f"{base}: A Practical Guide for Operators"
    return title if len(title) <= MAX_TITLE_CHARS else base
