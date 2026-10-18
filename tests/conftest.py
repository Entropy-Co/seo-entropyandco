from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from entropyseo.config import get_settings, load_niche_config
from entropyseo.schemas import BriefLink, CalendarCandidate, ContentBrief, SourceContext, VerifiedFact
from entropyseo.tools import framer_tools

ROOT = Path(__file__).resolve().parent.parent
CTA = "https://entropyand.co/contact"
SERVICE = "https://entropyand.co/services/ai-automation"
RELATED = "https://entropyand.co/blog/ai-agents-vs-automation"
NIST = "https://www.nist.gov/itl/ai-risk-management-framework"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Every test gets its own state/drafts dirs, no real keys, and fresh config caches."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DRAFTS_DIR", str(tmp_path / "drafts"))
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "SERPAPI_KEY",
        "FRAMER_API_KEY",
        "FRAMER_PROJECT_URL",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "LLM_PROVIDER",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NICHE_CONFIG_PATH", str(ROOT / "config" / "niche.yaml"))
    get_settings.cache_clear()
    load_niche_config.cache_clear()
    framer_tools.load_framer_config.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    load_niche_config.cache_clear()
    framer_tools.load_framer_config.cache_clear()


def candidate(
    title: str = "n8n vs Custom AI Agents: When Each One Pays Off", keyword: str = "n8n vs custom ai agents"
) -> CalendarCandidate:
    return CalendarCandidate(
        canonical_topic_id="n8n-custom",
        title=title,
        primary_keyword=keyword,
        intent="commercial",
        pillar="AI agents and workflow automation",
        dedupe_family="n8n-custom",
        evidence=["test"],
    )


def brief(**overrides: object) -> ContentBrief:
    excerpt = " ".join(["The framework helps organizations manage risks to individuals and society from AI."] * 60)
    data: dict[str, object] = {
        "topic": "n8n vs Custom AI Agents: When Each One Pays Off",
        "primary_keyword": "n8n vs custom ai agents",
        "calendar_candidate": candidate(),
        "target_length_words": 400,
        "min_words": 250,
        "max_words": 900,
        "min_faq_count": 2,
        "max_faq_count": 4,
        "links": [
            BriefLink(anchor="AI automation services", url=SERVICE, kind="internal"),
            BriefLink(anchor="agents versus automation", url=RELATED, kind="internal"),
            BriefLink(anchor="book a workflow audit", url=CTA, kind="internal"),
            BriefLink(anchor="NIST AI Risk Management Framework", url=NIST, kind="external"),
        ],
        "source_context": [SourceContext(title="AI RMF", url=NIST, source_type="standards", excerpt=excerpt)],
        "verified_facts": [VerifiedFact(fact="The AI RMF is voluntary.", source_url=NIST)],
        "cta_url": CTA,
    }
    data.update(overrides)
    return ContentBrief.model_validate(data)


def article_body(
    *, keyword: str = "n8n vs custom ai agents", dash: bool = False, cta: bool = True, extra_link: str = ""
) -> str:
    """A small article that satisfies the format contract for the test brief."""
    sep = " — " if dash else ". "
    sections_text = [
        (
            f"When does {keyword} matter for a small team?",
            "Most teams start with a visual workflow tool because it is quick to test. The cost shows up later when edge cases pile up.",
        ),
        (
            "What does each option cost to run each month?",
            "Assume a coordinator costs forty dollars an hour and spends six hours a week fixing failed runs. That is the number to beat.",
        ),
        (
            "Where do rules beat agents?",
            "Rules win when every input has the same fields. A form submission that always carries a name and email needs no judgment.",
        ),
        (
            "How should you test before you switch?",
            "Run the agent in shadow mode for two weeks. Compare its routing with what your team actually did and log every disagreement.",
        ),
        (
            "Rollout plan for the first ninety days",
            "Start with one queue, one owner, and a weekly review. Widen access only after error rates hold steady for a month.",
        ),
    ]
    sections = [f"## {heading}\n\n{text}\n" for heading, text in sections_text]
    closing_link = f"[book a workflow audit]({CTA})" if cta else "talk to us"
    extra = f" See [a vendor page]({extra_link}) too." if extra_link else ""
    return (
        f"Choosing between {keyword} comes down to how many exceptions your process has{sep}Rules are cheaper when "
        "inputs are predictable. Agents earn their cost when people currently read, judge, and route messy requests.\n\n"
        "## Key takeaways\n\n"
        "- Visual workflow tools win when inputs follow a fixed shape.\n"
        "- Custom agents pay off when staff spend hours judging unstructured requests.\n"
        "- Measure human interventions per week before and after any switch.\n\n"
        + "\n".join(sections)
        + "\n### A simple checklist\n\n- List every input source.\n- Count weekly exceptions.\n- Name one owner for escalations.\n\n"
        f"Our [AI automation services]({SERVICE}) page explains how we scope this, and the guide on "
        f"[agents versus automation]({RELATED}) covers the decision rule. The "
        f"[NIST AI Risk Management Framework]({NIST}) is a useful reference for review points.{extra}\n\n"
        f"## Where should you start?\n\nPick the workflow with the most manual exceptions and {closing_link} to map it "
        "before you build anything.\n\n"
        "## Frequently asked questions\n\n"
        "### Is a visual workflow tool enough for most small teams?\n\n"
        "For predictable inputs, yes. It becomes expensive when exceptions need judgment every day and someone must maintain many branches.\n\n"
        "### How long does a custom agent take to pilot?\n\n"
        "Plan a short pilot on one workflow with a human reviewing every output, then widen access once error rates are measured and stable.\n"
    )
