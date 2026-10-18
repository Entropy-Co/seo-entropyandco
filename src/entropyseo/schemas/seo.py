"""Typed contracts for every stage of the SEO pipeline.

`*Draft` models are LLM payloads (lenient, coerced). The non-draft models are the
canonical contracts the pipeline passes between nodes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from entropyseo.feeds import FeedKey

Intent = Literal["informational", "commercial", "transactional", "navigational"]
ContentType = Literal[
    "pillar-page", "how-to", "comparison", "checklist", "playbook", "explainer", "case-study", "guide"
]
BuyerStage = Literal["awareness", "consideration", "decision", "implementation"]
SourceType = Literal["first_party", "official_docs", "research", "standards", "news", "authority"]
PanelRole = Literal["seo_specialist", "eeat_reviewer", "humanization_auditor", "conversion_editor"]


def _validated_model_list[ModelT: BaseModel](value: object, model: type[ModelT]) -> list[ModelT]:
    """Keep valid entries from an LLM-produced list; drop malformed ones instead of failing the whole payload."""
    if not isinstance(value, list):
        return []
    out: list[ModelT] = []
    for item in value:
        if isinstance(item, model):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(model.model_validate(item))
            except ValidationError:
                continue
    return out


def _optional_literal(value: object, allowed: set[str]) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in allowed else None


# --- Research + planning -------------------------------------------------------------


class KeywordCluster(BaseModel):
    title: str
    description: str = ""
    pillar: str | None = None
    hub_term: str
    spokes: list[str] = Field(default_factory=list)
    content_type_hint: ContentType | None = None

    @field_validator("content_type_hint", mode="before")
    @classmethod
    def _coerce_type(cls, value: object) -> object:
        return _optional_literal(value, set(ContentType.__args__))  # type: ignore[attr-defined]


class KeywordResearchDraft(BaseModel):
    strategic_headline: str = ""
    clusters: list[KeywordCluster] = Field(default_factory=list)
    gap_notes: list[str] = Field(default_factory=list)

    @field_validator("clusters", mode="before")
    @classmethod
    def _coerce_clusters(cls, value: object) -> list[KeywordCluster]:
        return _validated_model_list(value, KeywordCluster)


class TrendSignal(BaseModel):
    title: str
    url: str
    source: Literal["hackernews", "web"] = "hackernews"
    points: int = 0
    comments: int = 0
    created_at: str = ""


class PublishedPost(BaseModel):
    """One article that already exists (live site, Framer CMS, or local ledger)."""

    title: str = ""
    slug: str
    url: str = ""
    published_at: str = ""
    source: Literal["sitemap", "blog_index", "framer", "ledger"] = "sitemap"


class ContentStrategyDecisionDraft(BaseModel):
    """LLM envelope for the strategy pick. Pool membership is checked separately in code."""

    decision: Literal["proceed", "skip"] = "skip"
    title: str = ""
    primary_keyword: str = ""
    pillar: str | None = None
    buyer_stage: BuyerStage | None = None
    content_type: ContentType | None = None
    angle: str = ""
    rationale: str = ""
    skip_reason: str | None = None

    @field_validator("buyer_stage", mode="before")
    @classmethod
    def _coerce_stage(cls, value: object) -> object:
        return _optional_literal(value, set(BuyerStage.__args__))  # type: ignore[attr-defined]

    @field_validator("content_type", mode="before")
    @classmethod
    def _coerce_type(cls, value: object) -> object:
        return _optional_literal(value, set(ContentType.__args__))  # type: ignore[attr-defined]

    @model_validator(mode="after")
    def _require_proceed_fields(self) -> ContentStrategyDecisionDraft:
        if self.decision == "proceed" and not (self.title.strip() and self.primary_keyword.strip()):
            raise ValueError("decision=proceed requires a non-empty title and primary_keyword")
        return self


class CalendarCandidate(BaseModel):
    """Canonical contract for one SEO calendar row's topic."""

    canonical_topic_id: str
    title: str
    primary_keyword: str
    intent: Intent | None = None
    pillar: str | None = None
    content_type: ContentType | None = None
    buyer_stage: BuyerStage | None = None
    angle: str = ""
    priority_score: float = 0.0
    dedupe_family: str
    evidence: list[str] = Field(default_factory=list)


# --- Brief --------------------------------------------------------------------------


class BriefStat(BaseModel):
    stat: str
    value: str
    source: str = Field(..., description="Source URL. 'needs_source' if none available.")


class BriefLink(BaseModel):
    anchor: str
    url: str
    kind: Literal["internal", "external"]


class SourceContext(BaseModel):
    title: str = ""
    url: str
    source_type: SourceType = "authority"
    excerpt: str = Field(
        default="",
        max_length=6000,
        description="Fetched source text that may support claims; title and URL alone are citation metadata.",
    )


class VerifiedFact(BaseModel):
    fact: str
    source_url: str
    source_title: str = ""


class ContentBrief(BaseModel):
    """Output of the research stage: the writer's only factual and link contract."""

    topic: str
    primary_keyword: str
    feed: FeedKey = "insights"
    calendar_candidate: CalendarCandidate | None = None
    run_date: str | None = None
    angle: str = ""
    target_reader: str = ""
    target_length_words: int
    min_words: int
    max_words: int
    min_faq_count: int
    max_faq_count: int | None = None
    summary_heading: str = "Key takeaways"
    h2_structure: list[str] = Field(default_factory=list)
    must_include_entities: list[str] = Field(default_factory=list)
    must_include_stats: list[BriefStat] = Field(default_factory=list)
    links: list[BriefLink] = Field(default_factory=list)
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    source_context: list[SourceContext] = Field(default_factory=list)
    cta_url: str = ""
    cta_required: bool = True
    source_policy: str = ""
    # False when no approved source page could be fetched; the writer still runs
    # from verified facts and clearly framed operator guidance.
    evidence_floor_enforced: bool = True
    serp_gap: str | None = None
    research_sources: list[str] = Field(default_factory=list)
    required_sections: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_contract(self) -> ContentBrief:
        if self.min_words > self.max_words:
            raise ValueError("min_words cannot exceed max_words")
        if self.max_faq_count is not None and self.max_faq_count < self.min_faq_count:
            raise ValueError("max_faq_count cannot be lower than min_faq_count")
        return self


class ContentBriefDraft(BaseModel):
    """LLM payload for brief generation before fixed fields are attached."""

    angle: str = ""
    target_reader: str = ""
    h2_structure: list[str] = Field(default_factory=list)
    must_include_entities: list[str] = Field(default_factory=list)
    must_include_stats: list[BriefStat] = Field(default_factory=list)
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    external_link_urls: list[str] = Field(default_factory=list)
    serp_gap: str | None = None
    required_sections: list[str] = Field(default_factory=list)

    @field_validator("must_include_stats", mode="before")
    @classmethod
    def _coerce_stats(cls, value: object) -> list[BriefStat]:
        return _validated_model_list(value, BriefStat)

    @field_validator("verified_facts", mode="before")
    @classmethod
    def _coerce_facts(cls, value: object) -> list[VerifiedFact]:
        return _validated_model_list(value, VerifiedFact)


# --- Draft --------------------------------------------------------------------------


class BlogDraftContent(BaseModel):
    """LLM payload for the writer before guard rails."""

    title: str = ""
    slug: str | None = None
    meta_title: str = ""
    meta_description: str = ""
    excerpt: str = ""
    body_markdown: str = ""
    keywords: list[str] = Field(default_factory=list)


class BlogArticleDraft(BaseModel):
    title: str
    slug: str
    meta_title: str = Field(..., min_length=10, max_length=60)
    meta_description: str = Field(..., min_length=80, max_length=160)
    excerpt: str = ""
    body_markdown: str
    primary_keyword: str
    keywords: list[str] = Field(default_factory=list)
    internal_links_used: list[BriefLink] = Field(default_factory=list)
    external_links_used: list[BriefLink] = Field(default_factory=list)
    word_count: int
    estimated_read_time_minutes: int
    humanization_passed: bool
    format_passed: bool
    format_report: dict[str, object] = Field(default_factory=dict)
    prompt_version: str
    generated_at: datetime


# --- Panel --------------------------------------------------------------------------


class ExpertScore(BaseModel):
    role: PanelRole
    score: int = Field(..., ge=0, le=100)
    critique: str
    must_fix: list[str] = Field(default_factory=list)

    @field_validator("role", mode="before")
    @classmethod
    def _normalize_role(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.strip().lower().replace("-", " ").replace("_", " ").split())
        aliases = {
            "seo": "seo_specialist",
            "seo specialist": "seo_specialist",
            "eeat reviewer": "eeat_reviewer",
            "e e a t reviewer": "eeat_reviewer",
            "humanization auditor": "humanization_auditor",
            "conversion editor": "conversion_editor",
            "commercial editor": "conversion_editor",
        }
        return aliases.get(normalized, value)


class PanelDraftResult(BaseModel):
    scores: list[ExpertScore] = Field(..., min_length=4, max_length=4)
    feedback_for_writer: str = ""

    @field_validator("scores", mode="before")
    @classmethod
    def _coerce_scores(cls, value: object) -> list[ExpertScore]:
        return _validated_model_list(value, ExpertScore)


class PanelResult(BaseModel):
    scores: list[ExpertScore]
    composite: float
    status: Literal["ship", "iterate", "hold"]
    round_number: int
    max_rounds: int
    next_steps: dict[str, object] = Field(default_factory=dict)
