"""Provider-agnostic LLM boundary: Anthropic or OpenAI behind one typed call.

Every model call in the pipeline goes through `structured(...)`, which returns a
validated Pydantic instance or raises. Provider choice is per tier (see
`Settings.provider_for`), so the writer and the judge can run on different
model families.

Provider rules handled here so agents never see them:
- Claude 5.x rejects sampling params (temperature/top_p); depth is controlled with
  `output_config.effort` instead. Agents pass an `effort` tier, never temperature.
- Anthropic structured output goes through `messages.parse(output_format=...)`;
  OpenAI through `responses.parse(text_format=...)` with `store=False`.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from entropyseo.config import get_settings, load_niche_config
from entropyseo.config.settings import Effort, LlmProvider, ModelTier

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output), keyed on a model-id substring. First match
# wins, so more specific keys come first. Unknown models fall back to the most
# expensive known rate so a typo can never slip under the cost cap.
_PRICING_PER_M: list[tuple[str, tuple[float, float]]] = [
    ("claude-fable", (10.0, 50.0)),
    ("claude-opus-5-5", (4.0, 20.0)),
    ("claude-opus", (5.0, 25.0)),
    ("claude-sonnet-5", (2.0, 10.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku-5", (0.10, 0.50)),
    ("claude-haiku", (1.0, 5.0)),
]
_PRICING_FALLBACK = (10.0, 50.0)
# Anthropic models that accept the server-side refusal fallback parameter.
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that reject output_config.effort.
_NO_EFFORT_SUBSTRINGS = ("haiku-4",)

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")
_SAFE_VERSION_RE = re.compile(r"^v\d+(\.\d+){0,2}$")


class LlmError(RuntimeError):
    """A model call returned no usable structured output."""


@dataclass
class CostLedger:
    """Accumulates token usage + USD across a run. Bind with `set_cost_ledger`."""

    entries: list[dict[str, int | str]] = field(default_factory=list)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.entries.append({"model": model, "input_tokens": int(input_tokens), "output_tokens": int(output_tokens)})

    @staticmethod
    def rates_for(model: str) -> tuple[float, float]:
        name = model.lower()
        overrides = get_settings().price_overrides()
        for key, rates in [*overrides.items(), *_PRICING_PER_M]:
            if key in name:
                return rates
        return _PRICING_FALLBACK

    @property
    def usd(self) -> float:
        total = 0.0
        for e in self.entries:
            in_rate, out_rate = self.rates_for(str(e["model"]))
            total += int(e["input_tokens"]) / 1_000_000 * in_rate
            total += int(e["output_tokens"]) / 1_000_000 * out_rate
        return total

    def summary(self) -> dict[str, float | int]:
        return {
            "usd": round(self.usd, 4),
            "input_tokens": sum(int(e["input_tokens"]) for e in self.entries),
            "output_tokens": sum(int(e["output_tokens"]) for e in self.entries),
            "calls": len(self.entries),
        }


_cost_ledger_ctx: contextvars.ContextVar[CostLedger | None] = contextvars.ContextVar(
    "entropyseo_cost_ledger", default=None
)


def set_cost_ledger(ledger: CostLedger | None) -> contextvars.Token[CostLedger | None]:
    return _cost_ledger_ctx.set(ledger)


def get_cost_ledger() -> CostLedger | None:
    return _cost_ledger_ctx.get()


def _record(model: str, input_tokens: int, output_tokens: int) -> None:
    ledger = get_cost_ledger()
    if ledger is not None:
        ledger.record(model, input_tokens, output_tokens)


def load_prompt(name: str, version: str = "v1.0") -> str:
    """Load src/entropyseo/prompts/<name>-<version>.md. Shipped prompts are immutable; add a new version."""
    if not _SAFE_NAME_RE.match(name) or not _SAFE_VERSION_RE.match(version):
        raise ValueError(f"Invalid prompt name/version: {name} {version}")
    path = _PROMPTS_DIR / f"{name}-{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt not found: {path}")
    return path.read_text(encoding="utf-8")


# --- Providers --------------------------------------------------------------------


@lru_cache(maxsize=1)
def _anthropic_client() -> Any:
    import anthropic

    s = get_settings()
    s.require("anthropic_api_key")
    return anthropic.Anthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_seconds, max_retries=2)


@lru_cache(maxsize=1)
def _openai_client() -> Any:
    from openai import OpenAI

    s = get_settings()
    s.require("openai_api_key")
    return OpenAI(api_key=s.openai_api_key, timeout=s.llm_timeout_seconds, max_retries=2)


def _anthropic_structured[ModelT: BaseModel](
    *, model: str, effort: Effort, system_prompt: str, user_content: str, schema: type[ModelT], max_tokens: int
) -> ModelT:
    s = get_settings()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "output_format": schema,
        # Writer retries and panel rounds resend the same system prompt + brief;
        # automatic caching makes those repeated prefixes cheap.
        "cache_control": {"type": "ephemeral"},
    }
    if not any(marker in model for marker in _NO_EFFORT_SUBSTRINGS):
        kwargs["output_config"] = {"effort": effort}
    betas: list[str] = []
    if s.anthropic_refusal_fallbacks and model in _FALLBACK_MODELS:
        betas.append(_FALLBACK_BETA)
        kwargs["fallbacks"] = "default"
    response = _anthropic_client().beta.messages.parse(**kwargs, **({"betas": betas} if betas else {}))

    usage = response.usage
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    # Price cache writes at 1.25x and reads at 0.1x of the input rate.
    input_equivalent = int(usage.input_tokens) + round(cache_write * 1.25) + round(cache_read * 0.1)
    _record(str(response.model or model), input_equivalent, int(usage.output_tokens))

    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise LlmError(f"{schema.__name__}: model declined the request (category={category})")
    if response.stop_reason == "max_tokens":
        raise LlmError(f"{schema.__name__}: output hit max_tokens={max_tokens} before completing")
    parsed = response.parsed_output
    if parsed is None:
        raise LlmError(f"{schema.__name__}: model returned no structured output")
    return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)


def _openai_effort(effort: Effort) -> str:
    # OpenAI reasoning effort has no xhigh/max; clamp to its top level.
    return {"xhigh": "high", "max": "high"}.get(effort, effort)


def _openai_structured[ModelT: BaseModel](
    *, model: str, effort: Effort, system_prompt: str, user_content: str, schema: type[ModelT], max_tokens: int
) -> ModelT:
    response = _openai_client().responses.parse(
        model=model,
        instructions=system_prompt,
        input=[{"role": "user", "content": user_content}],
        text_format=schema,
        max_output_tokens=max_tokens,
        reasoning={"effort": _openai_effort(effort)},
        store=False,
    )
    if response.usage is not None:
        _record(model, int(response.usage.input_tokens), int(response.usage.output_tokens))
    if response.status != "completed" or response.output_parsed is None:
        raise LlmError(f"{schema.__name__}: model did not return complete structured output (status={response.status})")
    return response.output_parsed


def structured[ModelT: BaseModel](
    *,
    system_prompt: str,
    user_content: str,
    schema: type[ModelT],
    tier: ModelTier = "default",
    max_tokens: int = 4000,
    effort: Effort | None = None,
) -> ModelT:
    """Call the configured provider for `tier` and return a validated `schema` instance."""
    s = get_settings()
    provider: LlmProvider = s.provider_for(tier)
    model = s.model_for(tier)
    chosen_effort = effort or s.effort_for(tier)
    call = _anthropic_structured if provider == "anthropic" else _openai_structured
    logger.debug("llm.call provider=%s tier=%s model=%s schema=%s", provider, tier, model, schema.__name__)
    return call(
        model=model,
        effort=chosen_effort,
        system_prompt=system_prompt,
        user_content=user_content,
        schema=schema,
        max_tokens=max_tokens,
    )


# --- Niche context -----------------------------------------------------------------


def build_niche_context_block(*, include_voice: bool = True, include_keywords: bool = False) -> str:
    """Format the niche config as a markdown block prepended to user content.

    Writers, the panel, and research all need the same authoritative brand context.
    Injecting it into the user turn keeps prompt files version-stable.
    """
    niche = load_niche_config()
    brand = niche.brand_entity
    lines: list[str] = ["## Entropy & Co. context (injected by pipeline)", ""]
    lines.append(f"- Brand: {brand.name} ({brand.url})")
    if brand.positioning:
        lines.append(f"- Positioning: {brand.positioning}")
    if brand.description:
        lines.append(f"- What we do: {brand.description}")
    claims = brand.verified_claims()
    if claims:
        lines.append("- Verified proof claims (the only company claims allowed):")
        lines.extend(f"  - {c.claim}" + (f" Source: {c.source_url}" if c.source_url else "") for c in claims)
    else:
        lines.append("- Verified proof claims: none. Make no claims about Entropy clients, results, or partnerships.")

    if niche.target_personas:
        lines.append("")
        lines.append("**Readers we write for:**")
        lines.extend(f"- {p.name} ({p.priority_level}): {p.description}" for p in niche.target_personas)
    if niche.pillars:
        lines.append("")
        lines.append("**Content pillars (priority order):**")
        lines.extend(
            f"- {p.name}" + (f": {p.description}" if p.description else "")
            for p in sorted(niche.pillars, key=lambda x: x.priority)
        )
    if include_keywords and niche.priority_keywords:
        lines.append("")
        lines.append("**Priority keywords:** " + ", ".join(kw.term for kw in niche.priority_keywords[:30]))
    if niche.banned_topics:
        lines.append("")
        lines.append("**Banned topics (never produce content about these):**")
        lines.extend(f"- {t}" for t in niche.banned_topics)
    if niche.prohibited_claims:
        lines.append("")
        lines.append("**Prohibited claims (never state or imply):**")
        lines.extend(f"- {t}" for t in niche.prohibited_claims)
    if niche.competitor_domains:
        lines.append("")
        lines.append("**Competitor domains (never link or promote):** " + ", ".join(niche.competitor_domains))
    if include_voice:
        voice = niche.voice
        lines.append("")
        lines.append("**Voice rules:**")
        lines.append(f"- Perspective: {voice.perspective}")
        if voice.tone:
            lines.append(f"- Tone: {voice.tone}")
        if voice.banned_characters:
            lines.append("- Banned characters (never emit): " + ", ".join(f"`{c}`" for c in voice.banned_characters))
        if voice.banned_phrases:
            lines.append("- Banned phrases (never emit): " + ", ".join(f"`{p}`" for p in voice.banned_phrases))
        lines.append(f"- Max paragraph length: {voice.max_paragraph_length_sentences} sentences")
    return "\n".join(lines) + "\n"


def inject_niche_context(user_content: str, *, include_voice: bool = True, include_keywords: bool = False) -> str:
    block = build_niche_context_block(include_voice=include_voice, include_keywords=include_keywords)
    return f"{block}\n---\n\n{user_content}"


def dumps(payload: object) -> str:
    """Stable JSON for prompts: sorted keys keep cached prefixes byte-identical across calls."""
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str)
