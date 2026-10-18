"""content-ops agent: four-role expert panel that scores a draft and decides ship / iterate / hold.

The deterministic format report is authoritative: a draft that failed it is held
without an LLM call. Scores are combined in code with fixed weights and per-role
minimums; the model never computes its own composite or status.
"""

from __future__ import annotations

import logging
from typing import Literal

from entropyseo.config import get_settings
from entropyseo.llm import dumps, inject_niche_context, load_prompt, structured
from entropyseo.schemas import BlogArticleDraft, ContentBrief, ExpertScore, PanelDraftResult, PanelResult

PROMPT = "content-ops"
VERSION = "v1.0"
logger = logging.getLogger(__name__)

COMPOSITE_TARGET = 88
PUBLISH_FLOOR = 80
PANEL_ATTEMPTS = 2
WEIGHTS: dict[str, float] = {
    "seo_specialist": 0.25,
    "eeat_reviewer": 0.30,
    "humanization_auditor": 0.25,
    "conversion_editor": 0.20,
}
ROLE_MINIMUMS: dict[str, int] = {
    "seo_specialist": 85,
    "eeat_reviewer": 85,
    "humanization_auditor": 90,
    "conversion_editor": 80,
}
type PanelStatus = Literal["ship", "iterate", "hold"]


def feedback_for_revision(panel: PanelResult | None) -> str | None:
    if panel is None:
        return None
    lines: list[str] = []
    if panel.next_steps.get("feedback_for_writer"):
        lines.append(str(panel.next_steps["feedback_for_writer"]))
    for score in panel.scores:
        lines.extend(f"{score.role}: {item}" for item in score.must_fix)
    return "\n".join(lines) or None


def composite(scores: list[ExpertScore]) -> float:
    return round(sum(s.score * WEIGHTS.get(s.role, 0.0) for s in scores), 1)


def score_misses(scores: list[ExpertScore], value: float) -> list[dict[str, object]]:
    by_role = {s.role: s.score for s in scores}
    misses: list[dict[str, object]] = [
        {"role": role, "score": by_role.get(role, 0), "minimum": minimum}
        for role, minimum in ROLE_MINIMUMS.items()
        if by_role.get(role, 0) < minimum
    ]
    if value < COMPOSITE_TARGET:
        misses.append({"role": "composite", "score": value, "minimum": COMPOSITE_TARGET})
    return misses


def _hold(round_number: int, reason: str, *, scores: list[ExpertScore] | None = None, **extra: object) -> PanelResult:
    return PanelResult(
        scores=scores or [ExpertScore(role=role, score=0, critique=reason) for role in WEIGHTS],  # type: ignore[arg-type]
        composite=0.0,
        status="hold",
        round_number=round_number,
        max_rounds=get_settings().panel_max_rounds,
        next_steps={"hard_hold": True, "hard_hold_reason": reason, **extra},
    )


def _policy() -> dict[str, object]:
    return {
        "weights": WEIGHTS,
        "role_minimums": ROLE_MINIMUMS,
        "composite_target": COMPOSITE_TARGET,
        "publish_floor": PUBLISH_FLOOR,
        "max_rounds": get_settings().panel_max_rounds,
    }


def run(draft: BlogArticleDraft, brief: ContentBrief, round_number: int = 1) -> PanelResult:
    max_rounds = get_settings().panel_max_rounds
    if not draft.format_passed:
        failed = draft.format_report.get("failed") if isinstance(draft.format_report, dict) else None
        return _hold(round_number, "deterministic_format_failure", failed=failed or [])

    payload = {
        "draft": draft.model_dump(mode="json", exclude={"format_report"}),
        "format_report_summary": {
            k: draft.format_report.get(k)
            for k in ("advisory_failed", "word_count", "keyword_uses", "question_h2_ratio")
        },
        "brief": brief.model_dump(mode="json", exclude={"calendar_candidate"}),
        "panel_policy": _policy(),
        "round_number": round_number,
    }
    parsed: PanelDraftResult | None = None
    error = ""
    # One retry: a malformed scorecard is a transient failure, and holding a draft
    # that already passed the deterministic contract on it wastes the whole run.
    for attempt in range(1, PANEL_ATTEMPTS + 1):
        try:
            parsed = structured(
                system_prompt=load_prompt(PROMPT, VERSION),
                user_content=inject_niche_context(dumps(payload)),
                schema=PanelDraftResult,
                tier="judge",
                max_tokens=get_settings().panel_max_tokens,
            )
            break
        except Exception as exc:
            error = str(exc)
            logger.warning("panel structured output failed attempt=%s: %s", attempt, exc)
    if parsed is None:
        return _hold(round_number, "panel_llm_failure", error=error)

    returned = {s.role for s in parsed.scores}
    if returned != set(WEIGHTS):
        return _hold(
            round_number,
            "incomplete_panel_output",
            scores=parsed.scores,
            missing_roles=sorted(set(WEIGHTS) - returned),
        )

    value = composite(parsed.scores)
    misses = score_misses(parsed.scores, value)
    next_steps: dict[str, object] = {"feedback_for_writer": parsed.feedback_for_writer}
    if not misses:
        status: PanelStatus = "ship"
    elif round_number < max_rounds:
        status = "iterate"
    elif value >= PUBLISH_FLOOR:
        # Final round at or above the floor ships, with the unresolved misses recorded
        # as advisory notes for the human reviewer.
        status = "ship"
        next_steps.update({"rating_advisory": True, "unresolved_panel_misses": misses})
    else:
        status = "hold"
        next_steps.update(
            {"hard_hold": True, "hard_hold_reason": "below_publish_floor", "unresolved_panel_misses": misses}
        )
    return PanelResult(
        scores=parsed.scores,
        composite=value,
        status=status,
        round_number=round_number,
        max_rounds=max_rounds,
        next_steps=next_steps,
    )
