from __future__ import annotations

from conftest import NIST, article_body, brief

from entropyseo.agents.seo_blog_writer import format_check, format_feedback
from entropyseo.schemas import BlogDraftContent
from entropyseo.tools.content_tools import humanization_check, title_to_slug

TITLE = "n8n vs Custom AI Agents: When Each One Pays Off"


def _content(body: str, **overrides: str) -> BlogDraftContent:
    return BlogDraftContent(
        meta_title=overrides.get("meta_title", "n8n vs Custom AI Agents: Cost and Fit"),
        meta_description=overrides.get(
            "meta_description",
            "Compare n8n vs custom ai agents by cost, maintenance, and exceptions so you can pick the right tool for each workflow.",
        ),
        body_markdown=body,
    )


def test_valid_article_passes_blocking_checks() -> None:
    report = format_check(brief(), title=TITLE, content=_content(article_body()))
    assert report["failed"] == [], report["failed"]
    assert report["faq"]["question_count"] == 2
    assert report["faq"]["is_last_h2"] is True
    assert len(report["internal_links_used"]) >= 2


def test_missing_cta_blocks() -> None:
    report = format_check(brief(), title=TITLE, content=_content(article_body(cta=False)))
    assert "has_cta" in report["failed"]
    assert "book a workflow audit" in format_feedback(report, brief()) or "contact" in format_feedback(report, brief())


def test_undeclared_link_blocks_and_feedback_names_it() -> None:
    report = format_check(brief(), title=TITLE, content=_content(article_body(extra_link="https://example.com/x")))
    assert "links_match_brief" in report["failed"]
    assert "https://example.com/x" in format_feedback(report, brief())


def test_faq_must_be_last_h2() -> None:
    body = article_body() + "\n## Appendix\n\nExtra notes that should not follow the FAQ.\n"
    report = format_check(brief(), title=TITLE, content=_content(body))
    assert "has_faqs" in report["failed"]


def test_short_meta_description_blocks_with_measured_length() -> None:
    report = format_check(brief(), title=TITLE, content=_content(article_body(), meta_description="Too short."))
    assert "meta_description_length" in report["failed"]
    assert "10 characters" in format_feedback(report, brief())


def test_external_citation_required_only_when_brief_offers_one() -> None:
    no_external = brief(links=[link for link in brief().links if link.url != NIST])
    body = article_body().replace(f"[NIST AI Risk Management Framework]({NIST})", "NIST guidance")
    assert "has_external_links" not in format_check(no_external, title=TITLE, content=_content(body))["failed"]


def test_humanization_flags_dashes_and_tells() -> None:
    result = humanization_check("We leverage AI — seamlessly.")
    assert not result["passed"]
    assert result["em_dashes"] == 1
    assert {"leverage", "seamless"} <= set(result["ai_tells_found"])
    assert humanization_check("Plain, specific sentences pass.")["passed"]


def test_slug() -> None:
    assert (
        title_to_slug("n8n vs Custom AI Agents: When Each One Pays Off")
        == "n8n-vs-custom-ai-agents-when-each-one-pays-off"
    )
