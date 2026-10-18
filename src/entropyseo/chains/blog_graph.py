"""LangGraph orchestrator for one scheduled blog run.

  guards ── capped ──────────────────────────────────────────────┐
     │                                                           │
  calendar ── queued row ─────────────────┐                      │
     │ (queue empty)                      │                      │
  plan: corpus → trends → keywords → strategy → enqueue + reserve │
     │ (skip) ────────────────────────────┼──────────────────────┤
     ▼                                    ▼                      │
                research → write → panel ─ iterate (≤ N rounds) ─┐
                                     │                           │
                          ship ──► persist_ship ──► publish gate │
                          hold ──► persist_hold                  │
                                     ▼                           ▼
                                  finalize (same calendar row, ledger, cost)

Design rules carried over from the BallersBank engine:
- persistence always runs, so every run leaves a reviewable artifact
- Framer is touched only when SEO_PUBLISH_ENABLED=true, DRY_RUN=false, and the panel shipped
- the run-level cost cap stops revision loops before they overspend
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph

from entropyseo.agents import content_ops, content_research_writer, content_strategy, keyword_research, seo_blog_writer
from entropyseo.chains import calendar
from entropyseo.config import get_settings, load_niche_config
from entropyseo.llm import CostLedger, get_cost_ledger, set_cost_ledger
from entropyseo.runtime_logging import log_event
from entropyseo.schemas import BlogArticleDraft, CalendarCandidate, ContentBrief, PanelResult
from entropyseo.tools import corpus_tools, drafts_tools, framer_tools, search_tools, structured_data

logger = logging.getLogger(__name__)
Record = dict[str, Any]


class BlogState(TypedDict, total=False):
    run_id: str
    run_date: str
    row: calendar.CalendarRow | None
    candidate: CalendarCandidate | None
    corpus_titles: list[str]
    strategy: Record
    brief: ContentBrief | None
    draft: BlogArticleDraft | None
    panel: PanelResult | None
    round_number: int
    status: str
    skip_reason: str
    persisted: Record
    publish: Record
    error: str


def _cost_usd() -> float:
    ledger = get_cost_ledger()
    return ledger.usd if ledger else 0.0


# --- Nodes -----------------------------------------------------------------------------


def node_guards(state: BlogState) -> Record:
    s = get_settings()
    if s.velocity_cap_per_day and len(drafts_tools.created_since(1)) >= s.velocity_cap_per_day:
        return {"status": "skipped", "skip_reason": f"daily velocity cap ({s.velocity_cap_per_day}) reached"}
    if s.velocity_cap_per_week and len(drafts_tools.created_since(7)) >= s.velocity_cap_per_week:
        return {"status": "skipped", "skip_reason": f"weekly velocity cap ({s.velocity_cap_per_week}) reached"}
    return {}


def node_calendar(state: BlogState) -> Record:
    row = calendar.reserve_next(state["run_id"])
    if row is None:
        return {"row": None}
    log_event(logger, logging.INFO, "blog.calendar.reserved", row=row.id, title=row.title)
    return {"row": row, "candidate": row.candidate}


def node_plan(state: BlogState) -> Record:
    corpus = corpus_tools.load_corpus()
    existing = [p.title for p in corpus if p.title] + calendar.all_titles()
    niche = load_niche_config()
    trends = search_tools.hackernews_trends(niche.trend_queries[:6]) if niche.trend_queries else []
    research = keyword_research.run()
    decision = content_strategy.run(
        candidates=research["candidates"], existing_titles=existing, trends=trends, clusters=research["clusters"]
    )
    summary = {
        "decision": decision["decision"],
        "rationale": decision.get("rationale"),
        "skip_reason": decision.get("skip_reason"),
        "candidate_count": len(research["candidates"]),
        "rejected_count": len(decision.get("rejected", [])),
        "signals": research["signals"],
    }
    if decision["decision"] != "proceed":
        return {"status": "skipped", "skip_reason": decision.get("skip_reason") or "strategy_skip", "strategy": summary}
    candidate = CalendarCandidate.model_validate(decision["calendar_candidate"])
    row = calendar.reserve(calendar.append(candidate), state["run_id"])
    log_event(logger, logging.INFO, "blog.strategy.queued", title=candidate.title, keyword=candidate.primary_keyword)
    return {"row": row, "candidate": candidate, "strategy": summary, "corpus_titles": existing}


def node_research(state: BlogState) -> Record:
    candidate = state["candidate"]
    assert candidate is not None
    brief = content_research_writer.run(candidate, corpus=corpus_tools.load_corpus(), run_date=state["run_date"])
    log_event(
        logger,
        logging.INFO,
        "blog.research.brief",
        sources=len(brief.source_context),
        facts=len(brief.verified_facts),
        links=len(brief.links),
        evidence_floor=brief.evidence_floor_enforced,
    )
    return {"brief": brief, "round_number": 0}


def node_write(state: BlogState) -> Record:
    brief = state["brief"]
    assert brief is not None
    draft = seo_blog_writer.run(
        brief,
        revision_feedback=content_ops.feedback_for_revision(state.get("panel")),
        draft_to_revise=state.get("draft"),
    )
    return {"draft": draft, "round_number": state.get("round_number", 0) + 1}


def node_panel(state: BlogState) -> Record:
    draft, brief = state["draft"], state["brief"]
    assert draft is not None and brief is not None
    panel = content_ops.run(draft, brief, round_number=state["round_number"])
    log_event(
        logger,
        logging.INFO,
        "blog.panel",
        round=f"{panel.round_number}/{panel.max_rounds}",
        status=panel.status,
        composite=panel.composite,
        scores="|".join(f"{s.role}={s.score}" for s in panel.scores),
        cost_usd=round(_cost_usd(), 3),
    )
    return {"panel": panel}


def _report(state: BlogState) -> Record:
    panel = state.get("panel")
    ledger = get_cost_ledger()
    return {
        "run_id": state["run_id"],
        "strategy": state.get("strategy"),
        "format_report": state["draft"].format_report if state.get("draft") else None,
        "panel": panel.model_dump(mode="json") if panel else None,
        "rounds": state.get("round_number", 0),
        "cost": ledger.summary() if ledger else None,
    }


def node_persist_ship(state: BlogState) -> Record:
    draft, brief = state["draft"], state["brief"]
    assert draft is not None and brief is not None
    persisted = drafts_tools.persist_draft(
        draft=draft, brief=brief, status="ship", report=_report(state), run_date=state["run_date"]
    )
    return {"persisted": persisted, "status": "drafted"}


def node_persist_hold(state: BlogState) -> Record:
    draft, brief = state["draft"], state["brief"]
    assert draft is not None and brief is not None
    persisted = drafts_tools.persist_draft(
        draft=draft, brief=brief, status="hold", report=_report(state), run_date=state["run_date"]
    )
    reason = (state.get("panel").next_steps.get("hard_hold_reason") if state.get("panel") else None) or "panel_hold"
    return {"persisted": persisted, "status": "held", "skip_reason": str(reason)}


def node_publish(state: BlogState) -> Record:
    s = get_settings()
    draft = state["draft"]
    assert draft is not None
    mode = s.framer_publish_mode
    try:
        brief = state["brief"]
        values = drafts_tools.framer_values(
            title=draft.title,
            slug=draft.slug,
            body_markdown=draft.body_markdown,
            excerpt=draft.excerpt,
            meta_title=draft.meta_title,
            meta_description=draft.meta_description,
            generated_at=draft.generated_at,
            read_time_minutes=draft.estimated_read_time_minutes,
            primary_keyword=draft.primary_keyword,
            keywords=draft.keywords,
            pillar=brief.calendar_candidate.pillar if brief and brief.calendar_candidate else None,
        )
        item = framer_tools.upsert_item(slug=draft.slug, values=values, draft=(mode == "draft"))
        result: Record = {"mode": mode, "item": item}
        if mode in {"preview", "live"}:
            preview = framer_tools.publish_preview()
            result["preview"] = preview
            if mode == "live":
                result["deploy"] = framer_tools.deploy(str(preview["deployment"]["id"]))
    except (framer_tools.FramerError, structured_data.StructuredDataError) as exc:
        log_event(logger, logging.ERROR, "blog.publish.failed", error=str(exc))
        return {"publish": {"mode": mode, "error": str(exc)}, "status": "drafted"}
    log_event(logger, logging.INFO, "blog.publish.ok", mode=mode, slug=draft.slug, item=item.get("id"))
    return {"publish": result, "status": "published" if mode == "live" else "drafted"}


def node_finalize(state: BlogState) -> Record:
    row = state.get("row")
    draft = state.get("draft")
    status = state.get("status") or "failed"
    result = {
        "run_id": state["run_id"],
        "status": status,
        "skip_reason": state.get("skip_reason"),
        "slug": draft.slug if draft else None,
        "url": drafts_tools.article_url(draft.slug) if draft else None,
        "panel_composite": state["panel"].composite if state.get("panel") else None,
        "artifact": (state.get("persisted") or {}).get("folder"),
        "publish": state.get("publish"),
        "cost": get_cost_ledger().summary() if get_cost_ledger() else None,
    }
    if row is not None:
        row_status = {"drafted": "drafted", "published": "published", "held": "held", "skipped": "skipped"}.get(
            status, "failed"
        )
        calendar.finalize(row, row_status, result)  # type: ignore[arg-type]
    if draft is not None and status in {"drafted", "published"}:
        drafts_tools.append_ledger(
            {"slug": draft.slug, "title": draft.title, "status": status, "run_id": state["run_id"]}
        )
    log_event(
        logger,
        logging.INFO,
        "blog.run.finished",
        **{k: v for k, v in result.items() if k in {"status", "slug", "skip_reason", "panel_composite"}},
    )
    return {"status": status}


# --- Gates -----------------------------------------------------------------------------


def gate_guards(state: BlogState) -> str:
    return "skip" if state.get("status") == "skipped" else "calendar"


def gate_calendar(state: BlogState) -> str:
    return "research" if state.get("row") else "plan"


def gate_plan(state: BlogState) -> str:
    return "skip" if state.get("status") == "skipped" else "research"


def gate_panel(state: BlogState) -> str:
    panel = state.get("panel")
    if panel is None or panel.status == "hold":
        return "hold"
    if panel.status == "ship":
        return "ship"
    if _cost_usd() >= get_settings().cost_cap_per_run_usd:
        log_event(logger, logging.WARNING, "blog.cost_cap.hit", cost_usd=round(_cost_usd(), 3))
        return "hold"
    return "iterate"


def gate_publish(state: BlogState) -> str:
    s = get_settings()
    if s.seo_publish_enabled and not s.dry_run and framer_tools.framer_configured():
        return "publish"
    return "finalize"


def build_graph() -> Any:
    g = StateGraph(BlogState)
    for name, fn in [
        ("guards", node_guards),
        ("calendar", node_calendar),
        ("plan", node_plan),
        ("research", node_research),
        ("write", node_write),
        ("panel", node_panel),
        ("persist_ship", node_persist_ship),
        ("persist_hold", node_persist_hold),
        ("publish", node_publish),
        ("finalize", node_finalize),
    ]:
        g.add_node(name, fn)
    g.set_entry_point("guards")
    g.add_conditional_edges("guards", gate_guards, {"skip": "finalize", "calendar": "calendar"})
    g.add_conditional_edges("calendar", gate_calendar, {"research": "research", "plan": "plan"})
    g.add_conditional_edges("plan", gate_plan, {"skip": "finalize", "research": "research"})
    g.add_edge("research", "write")
    g.add_edge("write", "panel")
    g.add_conditional_edges("panel", gate_panel, {"ship": "persist_ship", "hold": "persist_hold", "iterate": "write"})
    g.add_conditional_edges("persist_ship", gate_publish, {"publish": "publish", "finalize": "finalize"})
    g.add_edge("persist_hold", "finalize")
    g.add_edge("publish", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


def run_blog(*, run_date: str | None = None) -> Record:
    """Run one scheduled blog cycle end to end and return the final state summary."""
    ledger = CostLedger()
    token = set_cost_ledger(ledger)
    try:
        state: BlogState = {
            "run_id": uuid4().hex[:12],
            "run_date": run_date or datetime.now(UTC).date().isoformat(),
        }
        final = build_graph().invoke(state, {"recursion_limit": 40})
    finally:
        set_cost_ledger(None)
        del token
    draft = final.get("draft")
    return {
        "run_id": final.get("run_id"),
        "status": final.get("status"),
        "skip_reason": final.get("skip_reason"),
        "title": draft.title if draft else None,
        "slug": draft.slug if draft else None,
        "artifact": (final.get("persisted") or {}).get("folder"),
        "panel": final["panel"].model_dump(mode="json") if final.get("panel") else None,
        "publish": final.get("publish"),
        "strategy": final.get("strategy"),
        "cost": ledger.summary(),
    }
