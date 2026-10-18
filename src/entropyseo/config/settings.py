"""Centralized configuration loaded from env vars.

All agents and tools import settings from here; never read os.environ directly.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProvider = Literal["anthropic", "openai"]
ModelTier = Literal["default", "heavy", "fast", "judge"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
FramerPublishMode = Literal["draft", "preview", "live"]


class Settings(BaseSettings):
    """Typed runtime settings. Every env var the service needs is declared here."""

    model_config = SettingsConfigDict(
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Site ---
    site_url: str = "https://entropyand.co"
    blog_path_prefix: str = Field(default="/blog/", description="Public path prefix for blog articles.")
    blog_author: str = "Entropy & Co."

    # --- LLM provider ---
    # One switch picks the provider for every tier. Each tier can be pinned to the
    # other provider (e.g. judge on a different model family than the writer, so
    # the panel never grades its own output).
    llm_provider: LlmProvider = "anthropic"
    llm_provider_default: LlmProvider | None = None
    llm_provider_heavy: LlmProvider | None = None
    llm_provider_fast: LlmProvider | None = None
    llm_provider_judge: LlmProvider | None = None

    anthropic_api_key: str = ""
    anthropic_model_default: str = "claude-sonnet-5-5"
    anthropic_model_heavy: str = "claude-opus-5-5"
    anthropic_model_fast: str = "claude-haiku-5-5"
    anthropic_model_judge: str = "claude-sonnet-5-5"
    # Server-side refusal fallback (Claude API only): a declined request is re-run
    # on a fallback model inside the same call instead of failing the article.
    anthropic_refusal_fallbacks: bool = True

    openai_api_key: str = ""
    # No OpenAI defaults: model availability varies per account, so the operator
    # names an exact model id. OPENAI_MODEL covers every tier; per-tier ids override.
    openai_model: str = ""
    openai_model_default: str = ""
    openai_model_heavy: str = ""
    openai_model_fast: str = ""
    openai_model_judge: str = ""

    llm_effort_default: Effort = "medium"
    llm_effort_heavy: Effort = "high"
    llm_effort_fast: Effort = "low"
    llm_effort_judge: Effort = "medium"
    llm_timeout_seconds: float = Field(default=300.0, ge=10.0)
    # Optional JSON map of model-substring -> [input_usd_per_m, output_usd_per_m]
    # merged over the built-in price table used for the per-run cost cap.
    llm_price_overrides: str = ""

    # --- Research ---
    serpapi_key: str = ""
    serpapi_market: str = "us"
    google_service_account_json: str = Field(default="", description="Service account JSON or base64 JSON")
    gsc_site_url: str = "sc-domain:entropyand.co"
    http_user_agent: str = "EntropySEO/0.1 (+https://entropyand.co)"

    # --- Framer CMS ---
    framer_project_url: str = ""
    framer_api_key: str = ""
    framer_bridge_dir: str = "framer-bridge"
    framer_config_path: str = "config/framer.yaml"
    framer_publish_mode: FramerPublishMode = "draft"
    framer_bridge_timeout_seconds: int = Field(default=180, ge=10)

    # --- Storage ---
    state_dir: str = "state"
    drafts_dir: str = "drafts"

    # --- Observability ---
    log_level: str = "INFO"
    discord_report_webhook: str = ""

    # --- Safety ---
    dry_run: bool = True
    # When false the graph ends at persistence; it never calls Framer.
    seo_publish_enabled: bool = False
    cost_cap_per_run_usd: float = Field(default=10.0, ge=0.0)
    velocity_cap_per_week: int = Field(default=3, ge=0, description="0 disables the weekly cap.")
    velocity_cap_per_day: int = Field(default=1, ge=0, description="0 disables the daily cap.")
    topic_cooldown_days: int = Field(default=30, ge=0)
    blog_writer_max_tokens: int = Field(default=12000, ge=6000, le=32000)
    panel_max_tokens: int = Field(default=6000, ge=2000, le=16000)
    writer_max_retries: int = Field(default=3, ge=0, le=5)
    panel_max_rounds: int = Field(default=4, ge=1, le=6)

    @model_validator(mode="after")
    def _no_negative_cost_cap(self) -> Settings:
        if self.cost_cap_per_run_usd < 0:
            raise ValueError("cost_cap_per_run_usd cannot be negative")
        return self

    def judge_matches_writer(self) -> bool:
        """True when the panel would grade drafts with the model that wrote them (self-grading inflates scores)."""
        judge = self.model_for("judge", strict=False)
        return bool(judge) and (self.provider_for("judge"), judge) == (
            self.provider_for("heavy"),
            self.model_for("heavy", strict=False),
        )

    def provider_for(self, tier: ModelTier) -> LlmProvider:
        override = getattr(self, f"llm_provider_{tier}")
        return override or self.llm_provider

    def model_for(self, tier: ModelTier, *, strict: bool = True) -> str:
        provider = self.provider_for(tier)
        if provider == "anthropic":
            return getattr(self, f"anthropic_model_{tier}")
        model = getattr(self, f"openai_model_{tier}") or self.openai_model
        if not model and strict:
            raise RuntimeError(
                f"Tier {tier!r} uses OpenAI but no model is configured. Set OPENAI_MODEL (all tiers) "
                f"or OPENAI_MODEL_{tier.upper()} to an exact model id your account can use."
            )
        return model

    def effort_for(self, tier: ModelTier) -> Effort:
        return getattr(self, f"llm_effort_{tier}")

    def price_overrides(self) -> dict[str, tuple[float, float]]:
        if not self.llm_price_overrides.strip():
            return {}
        raw = json.loads(self.llm_price_overrides)
        return {str(key).lower(): (float(value[0]), float(value[1])) for key, value in raw.items()}

    def require(self, *attrs: str) -> None:
        """Raise if any required setting is missing; fail loud at boot, not deep in a tool call."""
        missing = [a for a in attrs if not getattr(self, a, None)]
        if missing:
            raise RuntimeError(f"Missing required settings: {', '.join(missing)}")

    def google_service_account_info(self) -> dict[str, object]:
        self.require("google_service_account_json")
        raw = self.google_service_account_json.strip()
        if raw.startswith("{"):
            return json.loads(raw)
        return json.loads(base64.b64decode(raw).decode("utf-8"))

    def site_host(self) -> str:
        from urllib.parse import urlparse

        return (urlparse(self.site_url).hostname or "").removeprefix("www.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
