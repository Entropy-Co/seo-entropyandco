"""Writer loop, panel decisions, research verification, calendar, LLM routing, and the full graph (LLM mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import NIST, article_body, brief, candidate

from entropyseo import llm
from entropyseo.agents import content_ops, content_research_writer, seo_blog_writer
from entropyseo.chains import blog_graph, calendar
from entropyseo.config import get_settings
from entropyseo.schemas import BlogDraftContent, BriefStat, ExpertScore, PanelDraftResult, SourceContext, VerifiedFact
from entropyseo.tools import framer_tools

META_DESCRIPTION = "Compare n8n vs custom ai agents by cost, maintenance, and exceptions so you can pick the right tool for each workflow."


def _draft_content(body: str) -> BlogDraftContent:
    return BlogDraftContent(
        meta_title="n8n vs Custom AI Agents: Cost and Fit",
        meta_description=META_DESCRIPTION,
        excerpt="Pick the right tool per workflow.",
        body_markdown=body,
        keywords=["n8n", "ai agents"],
    )


def _scores(value: int) -> PanelDraftResult:
    return PanelDraftResult(
        scores=[ExpertScore(role=r, score=value, critique="ok") for r in content_ops.WEIGHTS],  # type: ignore[arg-type]
        feedback_for_writer="tighten the cost section",
    )


def test_writer_revises_failed_draft_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_structured(**kwargs: Any) -> BlogDraftContent:
        calls.append(kwargs["user_content"])
        return _draft_content(article_body(dash=len(calls) == 1))

    monkeypatch.setattr(seo_blog_writer, "structured", fake_structured)
    draft = seo_blog_writer.run(brief())
    assert draft.format_passed and draft.humanization_passed
    assert len(calls) == 2
    assert "Humanization failed" in calls[1]
    assert "Previous draft (revise this; do not start over)" in calls[1]


def test_writer_evidence_hold_skips_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seo_blog_writer, "structured", lambda **k: pytest.fail("LLM must not be called"))
    draft = seo_blog_writer.run(brief(source_context=[], verified_facts=[]))
    assert not draft.format_passed
    assert "insufficient_source_evidence" in draft.format_report["failed"]


def test_panel_holds_failed_format_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(content_ops, "structured", lambda **k: pytest.fail("LLM must not be called"))
    draft = seo_blog_writer._failed(brief(), title="T title here", slug="t", failure="writer_retries_exhausted")
    result = content_ops.run(draft, brief())
    assert result.status == "hold" and result.next_steps["hard_hold_reason"] == "deterministic_format_failure"


@pytest.mark.parametrize(
    ("score", "round_number", "status"), [(95, 1, "ship"), (82, 1, "iterate"), (82, 4, "ship"), (70, 4, "hold")]
)
def test_panel_status_is_computed_in_code(
    monkeypatch: pytest.MonkeyPatch, score: int, round_number: int, status: str
) -> None:
    monkeypatch.setattr(seo_blog_writer, "structured", lambda **k: _draft_content(article_body()))
    draft = seo_blog_writer.run(brief())
    monkeypatch.setattr(content_ops, "structured", lambda **k: _scores(score))
    assert content_ops.run(draft, brief(), round_number=round_number).status == status


def test_research_keeps_only_supported_claims() -> None:
    source = SourceContext(
        url=NIST, excerpt="The AI RMF was released in January 2023 and is intended for voluntary use by organizations."
    )
    stats = [
        BriefStat(stat="AI RMF release", value="January 2023", source=NIST),
        BriefStat(stat="Adoption rate", value="73%", source=NIST),
        BriefStat(stat="Release", value="2023", source="https://other.example"),
    ]
    facts = [
        VerifiedFact(fact="The AI RMF is intended for voluntary use by organizations.", source_url=NIST),
        VerifiedFact(fact="Most enterprises have adopted autonomous procurement agents.", source_url=NIST),
    ]
    assert [s.value for s in content_research_writer.verify_stats(stats, [source])] == ["January 2023"]
    assert len(content_research_writer.verify_facts(facts, [source])) == 1


def test_calendar_lifecycle_and_stale_release() -> None:
    row = calendar.append(candidate())
    reserved = calendar.reserve_next("run1")
    assert reserved is not None and reserved.id == row.id and reserved.status == "posting"
    assert calendar.reserve_next("run2") is None
    rows = calendar.read_rows()
    rows[0].reserved_at = "2000-01-01T00:00:00+00:00"
    calendar.write_rows(rows)
    assert calendar.release_stale_rows() == 1
    final = calendar.finalize(calendar.reserve_next("run3"), "drafted", {"slug": "x"})  # type: ignore[arg-type]
    assert calendar.read_rows()[0].status == "drafted" and final.result == {"slug": "x"}


def test_llm_routes_per_tier_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER_JUDGE", "openai")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-model")
    get_settings.cache_clear()
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        llm, "_anthropic_structured", lambda **k: seen.setdefault("anthropic", k["model"]) and _scores(90)
    )
    monkeypatch.setattr(llm, "_openai_structured", lambda **k: seen.setdefault("openai", k["model"]) and _scores(90))
    llm.structured(system_prompt="s", user_content="u", schema=PanelDraftResult, tier="heavy")
    llm.structured(system_prompt="s", user_content="u", schema=PanelDraftResult, tier="judge")
    assert seen == {"anthropic": "claude-opus-5-5", "openai": "gpt-test-model"}


def test_openai_tier_without_model_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="OPENAI_MODEL"):
        get_settings().model_for("heavy")


def test_cost_ledger_prices_known_and_unknown_models() -> None:
    ledger = llm.CostLedger()
    ledger.record("claude-opus-5-5", 1_000_000, 100_000)
    assert round(ledger.usd, 2) == 6.0
    ledger.record("mystery-model", 1_000_000, 0)
    assert round(ledger.usd, 2) == 16.0  # unknown models priced at the most expensive known rate


def test_framer_values_follow_mapping() -> None:
    values = framer_tools.article_values(
        title="T",
        body_html="<p>x</p>",
        body_markdown="x",
        excerpt="e",
        meta_title="m",
        meta_description="d",
        published_date="2026-10-10",
        read_time_minutes=7,
    )
    assert values["Title"] == {"value": "T"}
    assert values["Content"] == {"value": "<p>x</p>", "contentType": "html"}
    assert values["Short Description"] == {"value": "e"}
    assert values["Meta Description"] == {"value": "d"}
    assert values["Category"] == {"value": "Article"}
    validation = framer_tools.validate_mapping(
        {"collections": [{"name": "Articles", "managedBy": "user", "fields": [{"name": "Title", "type": "string"}]}]}
    )
    assert validation["collection_found"] and "body" in validation["missing"]


def test_graph_end_to_end_with_mocked_agents(monkeypatch: pytest.MonkeyPatch, isolated_env: Path) -> None:
    calendar.append(candidate())
    monkeypatch.setattr(blog_graph.corpus_tools, "load_corpus", lambda **k: [])
    monkeypatch.setattr(blog_graph.content_research_writer, "run", lambda *a, **k: brief())
    monkeypatch.setattr(seo_blog_writer, "structured", lambda **k: _draft_content(article_body()))
    panel_rounds = iter([_scores(82), _scores(95)])
    monkeypatch.setattr(content_ops, "structured", lambda **k: next(panel_rounds))

    result = blog_graph.run_blog(run_date="2026-10-10")

    assert result["status"] == "drafted", result
    assert result["panel"]["round_number"] == 2
    folder = Path(result["artifact"])
    assert (folder / "article.md").exists() and (folder / "article.html").exists() and (folder / "body.md").exists()
    report = json.loads((folder / "report.json").read_text())
    preview = report["json_ld_preview"]
    assert [b["@type"] for b in preview["blocks"]] == ["Article", "FAQPage"]
    assert preview["problems"] == []
    assert 'target="_blank" rel="noopener noreferrer"' in (folder / "article.html").read_text()
    row = calendar.read_rows()[0]
    assert row.status == "drafted" and row.result["slug"] == result["slug"]
    ledger = (isolated_env / "state" / "ledger.jsonl").read_text()
    assert result["slug"] in ledger


def test_graph_respects_daily_velocity_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from entropyseo.tools import drafts_tools

    drafts_tools.append_ledger({"slug": "earlier", "title": "Earlier"})
    monkeypatch.setattr(blog_graph, "node_calendar", lambda s: pytest.fail("must not reserve a row"))
    result = blog_graph.run_blog()
    assert result["status"] == "skipped" and "daily velocity cap" in result["skip_reason"]
