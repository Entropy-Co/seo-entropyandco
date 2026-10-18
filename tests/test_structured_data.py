"""Article JSON-LD for the Framer Schema JSON-LD field: shape, checks, publish wiring, live-page parsing."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import brief, candidate

from entropyseo.chains import blog_graph
from entropyseo.schemas import BlogArticleDraft
from entropyseo.tools import drafts_tools, framer_tools, structured_data

URL = "https://entropyand.co/blog/n8n-vs-custom-ai-agents"
TITLE = "n8n vs Custom AI Agents: When Each One Pays Off"
GENERATED = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _values(**overrides: Any) -> dict[str, dict[str, Any]]:
    args: dict[str, Any] = {
        "title": TITLE,
        "slug": "n8n-vs-custom-ai-agents",
        "body_markdown": "Intro.\n\n## Section\n\nBody.",
        "excerpt": "Pick the right tool.",
        "meta_title": "n8n vs Custom AI Agents",
        "meta_description": "Compare n8n and custom AI agents by cost and fit.",
        "generated_at": GENERATED,
        "read_time_minutes": 6,
        "primary_keyword": "n8n vs custom ai agents",
        "keywords": ["N8N vs Custom AI Agents", "ai agents", "  workflow   automation "],
        "pillar": "AI agents and workflow automation",
        "now": NOW,
    }
    return drafts_tools.framer_values(**{**args, **overrides})


def test_framer_values_carry_schema_matching_existing_records() -> None:
    values = _values()
    ld = json.loads(values["Schema JSON-LD"]["value"])
    assert ld["@type"] == "Article" and ld["headline"] == TITLE
    assert ld["url"] == URL and ld["mainEntityOfPage"] == {"@type": "WebPage", "@id": URL}
    assert ld["datePublished"] == "2026-10-18T09:30:00.000Z" and ld["dateModified"] == "2026-10-19T12:00:00.000Z"
    assert ld["articleSection"] == "agentic-deployment"
    assert ld["isPartOf"] == {"@type": "Blog", "name": "Entropy & Co. Blog", "url": "https://entropyand.co/blog"}
    assert ld["inLanguage"] == "en-US"
    # Primary keyword first, case-insensitive dedupe, whitespace collapsed.
    assert ld["keywords"] == "n8n vs custom ai agents, ai agents, workflow automation"
    assert [a["name"] for a in ld["about"]] == ["n8n vs custom ai agents", "ai agents", "workflow automation"]
    assert "image" not in ld  # no cover image configured
    assert values["Topic Pillar"] == {"value": "agentic-deployment"}
    assert values["Target Keywords"] == {"value": ld["keywords"]}
    assert values["Updated At"] == {"value": ld["dateModified"]}
    assert values["Date"] == {"value": ld["datePublished"]}


def test_unmapped_pillar_passes_through_by_name() -> None:
    ld = json.loads(_values(pillar="Brand new pillar")["Schema JSON-LD"]["value"])
    assert ld["articleSection"] == "Brand new pillar"


def test_overlong_headline_blocks_the_framer_write() -> None:
    with pytest.raises(structured_data.StructuredDataError, match="110 or fewer"):
        _values(title="x" * 111)


def test_article_problems_name_each_defect() -> None:
    ld = structured_data.article_json_ld(
        title=TITLE,
        description="",
        url=URL,
        published_at="yesterday",
        modified_at=structured_data.iso_utc(NOW),
        section="",
        keywords=[],
    )
    problems = structured_data.article_problems(ld, title="Different title", url=URL + "/")
    joined = " | ".join(problems)
    for expected in ("description is empty", "does not match the article title", "mainEntityOfPage", "not an ISO 8601"):
        assert expected in joined
    assert structured_data.article_warnings(ld) == [
        "no image: Google recommends one for article rich results (set a cover image)",
        "no articleSection: the brief had no pillar",
    ]


def test_dumps_is_compact_and_script_safe() -> None:
    out = structured_data.dumps({"headline": "a </script> b", "name": "Entropy & Co."})
    assert out == '{"headline":"a <\\/script> b","name":"Entropy & Co."}'
    assert json.loads(out)["headline"] == "a </script> b"


def _page(*blocks: str) -> str:
    scripts = "".join(f'<script type="application/ld+json">{b}</script>' for b in blocks)
    return f"<html><head>{scripts}</head><body></body></html>"


ORG = '{"@context":"https://schema.org","@type":"Organization","name":"Entropy & Co."}'


def test_live_check_flags_pages_without_server_rendered_article() -> None:
    report = structured_data.live_check(_page(ORG), url=URL)
    assert not report["ok"] and "no Article JSON-LD" in report["problems"][0]
    assert report["types"] == ["Organization"]


def test_live_check_passes_the_field_value_and_flags_duplicates() -> None:
    article = _values()["Schema JSON-LD"]["value"]
    report = structured_data.live_check(_page(ORG, article), url=URL, title=TITLE)
    assert report["ok"], report["problems"]
    assert report["types"] == ["Organization", "Article"]
    assert any("no image" in w for w in report["warnings"])
    doubled = structured_data.live_check(_page(article, article), url=URL)
    assert "2 Article blocks" in doubled["problems"][0]
    broken = structured_data.live_check(_page("{not json"), url=URL)
    assert broken["problems"][0].startswith("invalid JSON-LD")


def _draft(title: str = TITLE) -> BlogArticleDraft:
    return BlogArticleDraft(
        title=title,
        slug="n8n-vs-custom-ai-agents",
        meta_title="n8n vs Custom AI Agents: Cost and Fit",
        meta_description="Compare n8n vs custom ai agents by cost, maintenance, and exceptions so you can pick the right tool.",
        body_markdown="Intro.\n\n## Section\n\nBody.",
        primary_keyword="n8n vs custom ai agents",
        keywords=["ai agents"],
        word_count=1200,
        estimated_read_time_minutes=6,
        humanization_passed=True,
        format_passed=True,
        prompt_version="test",
        generated_at=GENERATED,
    )


def test_publish_node_writes_schema_and_pillar(monkeypatch: pytest.MonkeyPatch) -> None:
    written: dict[str, Any] = {}

    def fake_upsert(*, slug: str, values: dict[str, Any], draft: bool) -> dict[str, Any]:
        written.update(slug=slug, values=values, draft=draft)
        return {"id": "item1", "slug": slug}

    monkeypatch.setattr(framer_tools, "upsert_item", fake_upsert)
    state: Any = {"draft": _draft(), "brief": brief(calendar_candidate=candidate())}
    result = blog_graph.node_publish(state)
    assert result["publish"]["item"]["id"] == "item1" and written["draft"] is True
    assert written["values"]["Topic Pillar"] == {"value": "agentic-deployment"}
    assert json.loads(written["values"]["Schema JSON-LD"]["value"])["headline"] == TITLE


def test_publish_node_refuses_bad_schema_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(framer_tools, "upsert_item", lambda **k: pytest.fail("must not write to Framer"))
    state: Any = {"draft": _draft(title="x" * 111), "brief": brief()}
    result = blog_graph.node_publish(state)
    assert "Article JSON-LD failed checks" in result["publish"]["error"]
    assert result["status"] == "drafted"
