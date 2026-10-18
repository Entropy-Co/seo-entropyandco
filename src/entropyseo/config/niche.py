"""Niche config loader: parses config/niche.yaml into typed models.

The niche config is the single source of truth for everything Entropy-specific:
positioning, pillars, seed keywords, personas, voice, approved sources, and
claims that must never appear. Agents read it at runtime.

Resolution order:
1. NICHE_CONFIG_PATH (authoritative when set)
2. config/niche.yaml relative to the working directory
3. config/niche.yaml relative to the repo root
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Intent = Literal["commercial", "transactional", "informational", "navigational"]


class PillarEntry(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    priority: int = 999
    service_url: str = ""


class PriorityKeyword(BaseModel):
    term: str = Field(..., min_length=1)
    intent: Intent
    pillar: str | None = None
    notes: str | None = None


class TargetPersona(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    priority_level: Literal["primary", "secondary", "tertiary"] = "secondary"


class VoiceRules(BaseModel):
    perspective: str = "first-person plural"
    tone: str = ""
    banned_phrases: list[str] = Field(default_factory=list)
    banned_characters: list[str] = Field(default_factory=list)
    max_paragraph_length_sentences: int = 4


class ProofClaim(BaseModel):
    claim: str = Field(..., min_length=1)
    source_url: str | None = None
    # Only verified claims reach a prompt. Unverified ones stay in config as a
    # visible to-do instead of being invented or silently dropped.
    verified: bool = False


class SocialProfile(BaseModel):
    platform: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)


class InternalPage(BaseModel):
    url: str = Field(..., min_length=1)
    anchor: str = Field(..., min_length=1)
    purpose: str = ""
    pillars: list[str] = Field(default_factory=list)


class BrandEntity(BaseModel):
    name: str = "Entropy & Co."
    url: str = "https://entropyand.co/"
    description: str = ""
    positioning: str = ""
    logo_url: str | None = None
    contact_email: str | None = None
    social_profiles: list[SocialProfile] = Field(default_factory=list)
    proof_claims: list[ProofClaim] = Field(default_factory=list)
    service_areas: list[str] = Field(default_factory=list)

    def verified_claims(self) -> list[ProofClaim]:
        return [claim for claim in self.proof_claims if claim.verified]


class NicheConfig(BaseModel):
    brand_entity: BrandEntity = Field(default_factory=BrandEntity)
    pillars: list[PillarEntry] = Field(default_factory=list)
    priority_keywords: list[PriorityKeyword] = Field(default_factory=list)
    target_personas: list[TargetPersona] = Field(default_factory=list)
    internal_pages: list[InternalPage] = Field(default_factory=list)
    trend_queries: list[str] = Field(default_factory=list)
    banned_topics: list[str] = Field(default_factory=list)
    prohibited_claims: list[str] = Field(default_factory=list)
    approved_external_domains: list[str] = Field(default_factory=list)
    competitor_domains: list[str] = Field(default_factory=list)
    seed_sources: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Pillar name -> authority URLs fetched when SerpAPI is unavailable.",
    )
    voice: VoiceRules = Field(default_factory=VoiceRules)

    def keyword_seeds(self) -> list[str]:
        if self.priority_keywords:
            return [kw.term for kw in self.priority_keywords]
        return [p.name for p in sorted(self.pillars, key=lambda x: x.priority)]

    def pillar_names(self) -> list[str]:
        return [p.name for p in sorted(self.pillars, key=lambda x: x.priority)]

    def pillar(self, name: str | None) -> PillarEntry | None:
        if not name:
            return None
        lowered = name.strip().lower()
        return next((p for p in self.pillars if p.name.lower() == lowered), None)


def _candidate_paths() -> list[Path]:
    explicit = os.environ.get("NICHE_CONFIG_PATH")
    if explicit:
        return [Path(explicit)]
    return [
        Path.cwd() / "config" / "niche.yaml",
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "niche.yaml",
    ]


@lru_cache(maxsize=1)
def load_niche_config() -> NicheConfig:
    """Load and validate the niche config. A malformed file raises; a missing one returns defaults."""
    for path in _candidate_paths():
        if path.is_file():
            with path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            return NicheConfig.model_validate(raw)
    return NicheConfig()
