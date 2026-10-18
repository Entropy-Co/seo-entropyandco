"""Local draft artifacts and the article ledger.

Every run writes a reviewable artifact whether or not it reaches Framer:
drafts/<date>-<slug>/ holds article.md (frontmatter + body), article.html (what
Framer receives), brief.json, and report.json (format checks, panel, cost).
Held drafts go under drafts/holds/. Created articles append to state/ledger.jsonl,
which feeds dedupe, velocity caps, and the corpus.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from markdown_it import MarkdownIt

from entropyseo.config import get_settings
from entropyseo.schemas import BlogArticleDraft, ContentBrief
from entropyseo.tools import framer_tools, structured_data

_MD = MarkdownIt("commonmark", {"html": False}).enable("table")
_EXTERNAL_LINK_RE = re.compile(r'<a href="(https?://[^"]+)"')
_FAQ_RE = re.compile(r"^##\s+(?:Frequently asked questions|FAQs?)\s*$", re.IGNORECASE | re.MULTILINE)
_H3_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)


def render_html(body_markdown: str) -> str:
    """Markdown -> HTML for Framer formatted text. External links open safely in a new tab."""
    html = _MD.render(body_markdown)
    site_host = get_settings().site_host()

    def _attrs(match: re.Match[str]) -> str:
        url = match.group(1)
        host = (urlparse(url).hostname or "").removeprefix("www.")
        if host == site_host or host.endswith(f".{site_host}"):
            return match.group(0)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer"'

    return _EXTERNAL_LINK_RE.sub(_attrs, html)


def faq_pairs(body_markdown: str) -> list[tuple[str, str]]:
    match = _FAQ_RE.search(body_markdown)
    if not match:
        return []
    section = body_markdown[match.end() :]
    next_h2 = re.search(r"^##\s+", section, re.MULTILINE)
    section = section[: next_h2.start()] if next_h2 else section
    questions = list(_H3_RE.finditer(section))
    pairs: list[tuple[str, str]] = []
    for i, q in enumerate(questions):
        end = questions[i + 1].start() if i + 1 < len(questions) else len(section)
        answer = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", section[q.end() : end]).strip()
        pairs.append((q.group(1).strip(), " ".join(answer.split())))
    return pairs


def json_ld_preview(draft: BlogArticleDraft, url: str, pillar: str | None = None) -> dict[str, Any]:
    """The Article JSON-LD Framer will receive, its check results, and an FAQPage block for review."""
    published = structured_data.iso_utc(draft.generated_at)
    article = structured_data.article_json_ld(
        title=draft.title,
        description=draft.meta_description,
        url=url,
        published_at=published,
        modified_at=published,
        section=framer_tools.pillar_label(pillar),
        keywords=structured_data.keyword_list(draft.primary_keyword, draft.keywords),
        image_url=framer_tools.load_framer_config().default_cover_image_url,
    )
    blocks: list[dict[str, Any]] = [article]
    pairs = faq_pairs(draft.body_markdown)
    if pairs:
        blocks.append(
            {
                "@context": "https://schema.org",
                "@type": "FAQPage",
                "mainEntity": [
                    {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": escape(a)}}
                    for q, a in pairs
                ],
            }
        )
    return {
        "blocks": blocks,
        "problems": structured_data.article_problems(article, title=draft.title, url=url),
        "warnings": structured_data.article_warnings(article),
    }


def framer_values(
    *,
    title: str,
    slug: str,
    body_markdown: str,
    excerpt: str,
    meta_title: str,
    meta_description: str,
    generated_at: datetime,
    read_time_minutes: int,
    primary_keyword: str,
    keywords: list[str],
    pillar: str | None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Framer field values for one article, including checked Article JSON-LD.

    Raises structured_data.StructuredDataError when the JSON-LD fails its checks.
    """
    config = framer_tools.load_framer_config()
    published = structured_data.iso_utc(generated_at)
    modified = structured_data.iso_utc(now or datetime.now(UTC))
    section = framer_tools.pillar_label(pillar)
    terms = structured_data.keyword_list(primary_keyword, keywords)
    ld = structured_data.checked_article_json_ld(
        title=title,
        url=article_url(slug),
        description=meta_description,
        published_at=published,
        modified_at=modified,
        section=section,
        keywords=terms,
        image_url=config.default_cover_image_url,
    )
    return framer_tools.article_values(
        title=title,
        # Rendered here so edits a reviewer made to body.md are what Framer receives.
        body_html=render_html(body_markdown),
        body_markdown=body_markdown,
        excerpt=excerpt,
        meta_title=meta_title,
        meta_description=meta_description,
        published_date=published,
        read_time_minutes=read_time_minutes,
        updated_at=modified,
        topic_pillar=section,
        target_keywords=", ".join(terms),
        schema_json_ld=structured_data.dumps(ld),
    )


def article_url(slug: str) -> str:
    s = get_settings()
    return f"{s.site_url.rstrip('/')}/{s.blog_path_prefix.strip('/')}/{slug}"


def persist_draft(
    *,
    draft: BlogArticleDraft,
    brief: ContentBrief,
    status: str,
    report: dict[str, Any],
    run_date: str,
) -> dict[str, str]:
    """Write the run's artifacts. `status` is ship or hold; holds go to drafts/holds/."""
    s = get_settings()
    base = Path(s.drafts_dir) / ("holds" if status == "hold" else "")
    folder = base / f"{run_date}-{draft.slug}"
    folder.mkdir(parents=True, exist_ok=True)
    url = article_url(draft.slug)
    frontmatter = {
        "title": draft.title,
        "slug": draft.slug,
        "url": url,
        "status": status,
        "primary_keyword": draft.primary_keyword,
        "meta_title": draft.meta_title,
        "meta_description": draft.meta_description,
        "excerpt": draft.excerpt,
        "keywords": draft.keywords,
        "word_count": draft.word_count,
        "read_time_minutes": draft.estimated_read_time_minutes,
        "prompt_version": draft.prompt_version,
        "generated_at": draft.generated_at.isoformat(),
        "pillar": brief.calendar_candidate.pillar if brief.calendar_candidate else None,
    }
    (folder / "article.md").write_text(
        f"---\n{yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)}---\n\n# {draft.title}\n\n{draft.body_markdown}\n",
        encoding="utf-8",
    )
    (folder / "body.md").write_text(draft.body_markdown, encoding="utf-8")
    (folder / "article.html").write_text(render_html(draft.body_markdown), encoding="utf-8")
    (folder / "brief.json").write_text(brief.model_dump_json(indent=2), encoding="utf-8")
    report = {**report, "json_ld_preview": json_ld_preview(draft, url, frontmatter["pillar"])}
    (folder / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return {"folder": str(folder), "markdown": str(folder / "article.md"), "html": str(folder / "article.html")}


def _ledger_path() -> Path:
    return Path(get_settings().state_dir) / "ledger.jsonl"


def append_ledger(row: dict[str, Any]) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"created_at": datetime.now(UTC).isoformat(), **row}, ensure_ascii=False) + "\n")


def ledger_rows() -> list[dict[str, Any]]:
    path = _ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def created_since(days: int, *, today: date | None = None) -> list[dict[str, Any]]:
    """Ledger rows created within the last `days` days (for velocity caps and cooldown)."""
    cutoff = datetime.combine(today or date.today(), datetime.min.time(), tzinfo=UTC) - timedelta(days=days - 1)
    out = []
    for row in ledger_rows():
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (KeyError, ValueError):
            continue
        if created >= cutoff:
            out.append(row)
    return out
