"""Article format policy for the Entropy & Co. blog.

One feed today (long-form operator guides). The policy object is the feed
contract, so a second feed (e.g. comparison pages or playbooks) can be added
without touching the format checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FeedKey = Literal["insights"]


@dataclass(frozen=True)
class ArticlePolicy:
    key: FeedKey
    label: str
    target_length_words: int
    min_words: int
    max_words: int
    min_faq_count: int
    max_faq_count: int | None
    summary_heading: str = "Key takeaways"
    min_h2_count: int = 5
    min_summary_bullets: int = 3
    max_summary_bullets: int = 5
    min_lead_words: int = 25
    max_lead_words: int = 90
    min_lead_sentences: int = 2
    max_lead_sentences: int = 4
    min_primary_keyword_uses: int = 2
    max_primary_keyword_uses: int = 12
    min_question_h2_ratio: float = 0.35
    max_avg_sentence_words: float = 21.0
    max_long_word_ratio: float = 0.24
    # Advisory misses are fed back to the writer but never block a draft.
    advisory_format_checks: frozenset[str] = frozenset()
    min_evidence_words: int = 400


def insights_policy() -> ArticlePolicy:
    return ArticlePolicy(
        key="insights",
        label="Entropy & Co. blog",
        target_length_words=2200,
        min_words=1800,
        max_words=2700,
        min_faq_count=5,
        max_faq_count=8,
        advisory_format_checks=frozenset(
            {
                "word_count_range",
                "keyword_in_h2",
                "keyword_not_stuffed",
                "lead_is_concise",
                "question_h2_ratio",
                "plain_language",
            }
        ),
    )


def policy_for_feed(feed: FeedKey = "insights") -> ArticlePolicy:
    return insights_policy()
