"""Published-corpus inventory: what already exists on entropyand.co.

Sources, merged by slug:
- public sitemap (authoritative list of live blog paths)
- public blog index (human titles for those paths)
- Framer CMS items, including drafts (when the bridge is configured)
- local ledger of articles this engine created

The merged corpus feeds topic dedupe and internal-link selection. It is cached in
state/corpus.json and refreshed when older than `max_age_hours`.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser

from entropyseo.config import get_settings
from entropyseo.schemas import PublishedPost
from entropyseo.tools.web_tools import fetch_text

logger = logging.getLogger(__name__)

_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


def _corpus_path() -> Path:
    return Path(get_settings().state_dir) / "corpus.json"


def _blog_url(slug: str) -> str:
    s = get_settings()
    return urljoin(s.site_url.rstrip("/") + "/", f"{s.blog_path_prefix.strip('/')}/{slug}")


def slug_from_url(url: str) -> str:
    prefix = get_settings().blog_path_prefix.strip("/")
    path = urlparse(url).path.strip("/")
    if not path.startswith(f"{prefix}/"):
        return ""
    return path[len(prefix) + 1 :].strip("/")


def title_from_slug(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.replace("-", " ").split())


def sitemap_posts() -> list[PublishedPost]:
    xml = fetch_text(f"{get_settings().site_url.rstrip('/')}/sitemap.xml")
    posts: list[PublishedPost] = []
    for loc in _LOC_RE.findall(xml):
        slug = slug_from_url(loc)
        if slug:
            posts.append(PublishedPost(slug=slug, url=loc, title=title_from_slug(slug), source="sitemap"))
    return posts


_KNOWN_CARD_LABELS = {"Article", "Articles", "Resource", "Resources", "Guide", "Guides", "Playbook", "News", "Insights"}


def _strip_category_labels(titles: dict[str, str]) -> dict[str, str]:
    """Card anchors read "Article <title>". Drop a leading category label: a known one, or a word prefixing many cards."""
    first_words = Counter(t.split(" ", 1)[0] for t in titles.values() if " " in t)
    labels = _KNOWN_CARD_LABELS | {w for w, n in first_words.items() if n >= max(3, len(titles) // 20) and len(w) <= 14}
    out: dict[str, str] = {}
    for slug, title in titles.items():
        head, _, rest = title.partition(" ")
        out[slug] = rest if head in labels and rest else title
    return out


def blog_index_titles() -> dict[str, str]:
    html = fetch_text(f"{get_settings().site_url.rstrip('/')}{get_settings().blog_path_prefix.rstrip('/')}")
    if not html:
        return {}
    tree = LexborHTMLParser(html)
    titles: dict[str, str] = {}
    for anchor in tree.css("a[href]"):
        slug = slug_from_url(urljoin(get_settings().site_url + "/", str(anchor.attributes.get("href") or "")))
        if not slug or slug in titles:
            continue
        heading = anchor.css_first("h1, h2, h3, h4")
        text = heading.text(strip=True) if heading else anchor.text(separator=" ", strip=True)
        text = " ".join(text.split())
        if 10 <= len(text) <= 200:
            titles[slug] = text
    return _strip_category_labels(titles)


def ledger_posts() -> list[PublishedPost]:
    path = Path(get_settings().state_dir) / "ledger.jsonl"
    if not path.exists():
        return []
    posts: list[PublishedPost] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("slug"):
            posts.append(
                PublishedPost(
                    slug=row["slug"],
                    title=row.get("title", ""),
                    url=row.get("url") or _blog_url(row["slug"]),
                    published_at=row.get("created_at", ""),
                    source="ledger",
                )
            )
    return posts


def framer_posts() -> list[PublishedPost]:
    """Items in the configured Framer blog collection, drafts included. [] when Framer is not configured."""
    from entropyseo.tools import framer_tools

    if not framer_tools.framer_configured():
        return []
    try:
        items = framer_tools.list_items()
    except framer_tools.FramerError as exc:
        logger.warning("framer corpus read failed: %s", exc)
        return []
    return [
        PublishedPost(
            slug=item["slug"],
            title=item.get("title") or title_from_slug(item["slug"]),
            url=_blog_url(item["slug"]),
            source="framer",
        )
        for item in items
        if item.get("slug")
    ]


def build_corpus() -> list[PublishedPost]:
    by_slug: dict[str, PublishedPost] = {}
    for post in [*sitemap_posts(), *framer_posts(), *ledger_posts()]:
        existing = by_slug.get(post.slug)
        if existing is None:
            by_slug[post.slug] = post
        elif existing.title == title_from_slug(existing.slug) and post.title:
            # Keep the first source's identity but prefer a real headline over a slug-derived one.
            by_slug[post.slug] = existing.model_copy(update={"title": post.title})
    for slug, title in blog_index_titles().items():
        if slug in by_slug:
            by_slug[slug] = by_slug[slug].model_copy(update={"title": title})
        else:
            by_slug[slug] = PublishedPost(slug=slug, title=title, url=_blog_url(slug), source="blog_index")
    return sorted(by_slug.values(), key=lambda p: p.slug)


def load_corpus(*, refresh: bool = False, max_age_hours: int = 24) -> list[PublishedPost]:
    """Cached corpus; rebuilt when stale, forced, or missing. A failed rebuild falls back to the cache."""
    path = _corpus_path()
    if path.exists() and not refresh:
        cached = json.loads(path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(cached.get("fetched_at", "1970-01-01T00:00:00+00:00"))
        if datetime.now(UTC) - fetched_at < timedelta(hours=max_age_hours):
            return [PublishedPost.model_validate(p) for p in cached.get("posts", [])]
    posts = build_corpus()
    if not posts and path.exists():
        logger.warning("corpus rebuild returned nothing; using cached corpus")
        return [PublishedPost.model_validate(p) for p in json.loads(path.read_text(encoding="utf-8")).get("posts", [])]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"fetched_at": datetime.now(UTC).isoformat(), "posts": [p.model_dump(mode="json") for p in posts]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return posts
