"""Article JSON-LD for the Framer "Schema JSON-LD" field, built and checked in code.

The shape matches the 129 existing records in the Articles collection (Article,
mainEntityOfPage, isPartOf Blog, articleSection = Topic Pillar id, about terms) so
the blog template renders new and old posts the same way. Nothing here calls a model.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from entropyseo.config import get_settings
from entropyseo.tools import framer_tools

# Google truncates longer headlines in article rich results.
MAX_HEADLINE_CHARS = 110
_LD_SCRIPT_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)


def iso_utc(value: datetime) -> str:
    """Framer's date format: UTC with milliseconds and a Z suffix."""
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def keyword_list(primary_keyword: str, keywords: list[str]) -> list[str]:
    """Primary keyword first, then the draft's keywords, deduplicated case-insensitively."""
    seen: set[str] = set()
    out: list[str] = []
    for term in [primary_keyword, *keywords]:
        clean = " ".join(term.split())
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            out.append(clean)
    return out


def article_json_ld(
    *,
    title: str,
    description: str,
    url: str,
    published_at: str,
    modified_at: str,
    section: str,
    keywords: list[str],
    image_url: str = "",
) -> dict[str, Any]:
    s = get_settings()
    schema = framer_tools.load_framer_config().schema_
    org = {"@type": "Organization", "name": s.blog_author, "url": s.site_url}
    blog_url = f"{s.site_url.rstrip('/')}/{s.blog_path_prefix.strip('/')}"
    ld: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": description,
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "datePublished": published_at,
        "dateModified": modified_at,
        "author": org,
        "publisher": org,
        "inLanguage": schema.language,
        "isPartOf": {"@type": "Blog", "name": schema.blog_name or f"{s.blog_author} Blog", "url": blog_url},
    }
    if image_url:
        ld["image"] = [image_url]
    if section:
        ld["articleSection"] = section
    if keywords:
        ld["keywords"] = ", ".join(keywords)
        ld["about"] = [{"@type": "Thing", "name": k} for k in keywords[: schema.max_about_terms]]
    return ld


def article_problems(ld: dict[str, Any], *, title: str, url: str) -> list[str]:
    """Blocking defects: anything here means the JSON-LD must not reach Framer."""
    problems: list[str] = []
    if ld.get("@type") not in {"Article", "BlogPosting"}:
        problems.append(f"@type is {ld.get('@type')!r}, expected Article")
    for key in ("headline", "description", "url", "datePublished", "dateModified"):
        if not str(ld.get(key) or "").strip():
            problems.append(f"{key} is empty")
    headline = str(ld.get("headline") or "")
    if headline != title:
        problems.append(f"headline {headline!r} does not match the article title {title!r}")
    if len(headline) > MAX_HEADLINE_CHARS:
        problems.append(f"headline is {len(headline)} characters; keep it at {MAX_HEADLINE_CHARS} or fewer")
    if ld.get("url") != url or (ld.get("mainEntityOfPage") or {}).get("@id") != url:
        problems.append(f"url and mainEntityOfPage must both be {url}")
    for key in ("datePublished", "dateModified"):
        try:
            datetime.fromisoformat(str(ld.get(key)).replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"{key} {ld.get(key)!r} is not an ISO 8601 date")
    for key in ("author", "publisher"):
        if not (ld.get(key) or {}).get("name"):
            problems.append(f"{key}.name is empty")
    return problems


def article_warnings(ld: dict[str, Any]) -> list[str]:
    """Non-blocking gaps a reviewer should see."""
    warnings: list[str] = []
    if not ld.get("image"):
        warnings.append("no image: Google recommends one for article rich results (set a cover image)")
    if not ld.get("articleSection"):
        warnings.append("no articleSection: the brief had no pillar")
    return warnings


class StructuredDataError(ValueError):
    """Article JSON-LD failed its checks; the article must not be written to Framer."""


def checked_article_json_ld(*, title: str, url: str, **kwargs: Any) -> dict[str, Any]:
    ld = article_json_ld(title=title, url=url, **kwargs)
    problems = article_problems(ld, title=title, url=url)
    if problems:
        raise StructuredDataError("Article JSON-LD failed checks: " + "; ".join(problems))
    return ld


def dumps(ld: dict[str, Any]) -> str:
    """Compact JSON, like the existing records. `</` is escaped so the string is safe inside a script tag."""
    return json.dumps(ld, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def page_json_ld(html: str) -> list[dict[str, Any]]:
    """Every JSON-LD object in raw (unrendered) HTML. Unparseable blocks become {"_error": ...}."""
    out: list[dict[str, Any]] = []
    for raw in _LD_SCRIPT_RE.findall(html):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            out.append({"_error": f"invalid JSON-LD: {exc}"})
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out.extend(i for i in items if isinstance(i, dict))
    return out


def live_check(html: str, *, url: str, title: str = "") -> dict[str, Any]:
    """What a crawler that does not run JavaScript sees on a published article."""
    blocks = page_json_ld(html)
    articles = [b for b in blocks if b.get("@type") in {"Article", "BlogPosting"}]
    problems: list[str] = [b["_error"] for b in blocks if "_error" in b]
    if not articles:
        problems.append("no Article JSON-LD in the server HTML (is the Schema JSON-LD field rendered by the template?)")
    elif len(articles) > 1:
        problems.append(f"{len(articles)} Article blocks; expected exactly one")
    else:
        problems += article_problems(articles[0], title=title or str(articles[0].get("headline") or ""), url=url)
    return {
        "url": url,
        "types": [b.get("@type") for b in blocks if "_error" not in b],
        "ok": not problems,
        "problems": problems,
        "warnings": article_warnings(articles[0]) if len(articles) == 1 else [],
    }
