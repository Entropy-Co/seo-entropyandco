"""Web fetching and source-text extraction for research.

Plain HTTPS fetches only (no unlocker/proxy). A blocked or empty page yields an
empty excerpt, and the brief records that the evidence floor could not be met.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from entropyseo.config import get_settings

logger = logging.getLogger(__name__)

_BODY_SELECTORS = ("article", "main", "[role='main']", ".article-content", ".entry-content", ".post-content")
_NOISE_SELECTORS = ("script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg")
_BLOCK_MARKERS = ("cf-chl-", "cloudflare ray id", "enable javascript and cookies to continue", "verify you are human")


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def host_matches(url: str, domains: list[str] | tuple[str, ...]) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith(f".{d}") for d in (x.lower().removeprefix("www.") for x in domains))


def fetch_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _BLOCK_MARKERS)


def extract_source_excerpt(html: str, max_chars: int = 5000) -> str:
    """Extract bounded article/main text from fetched HTML without site chrome."""
    if max_chars < 1 or not html.strip() or re.match(r"\s*<(?:rss|feed|\?xml)\b", html, re.IGNORECASE):
        return ""
    if fetch_blocked(html):
        return ""
    tree = HTMLParser(html)
    for selector in _NOISE_SELECTORS:
        for node in tree.css(selector):
            node.decompose()
    candidates: list[str] = []
    for selector in _BODY_SELECTORS:
        parts = [" ".join(node.text(separator=" ", strip=True).split()) for node in tree.css(selector)]
        text = " ".join(dict.fromkeys(p for p in parts if p))
        if text:
            candidates.append(text)
    paragraphs = [
        " ".join(node.text(separator=" ", strip=True).split())
        for node in tree.css("p")
        if len(node.text(separator=" ", strip=True).split()) >= 8
    ]
    if paragraphs:
        candidates.append(" ".join(dict.fromkeys(paragraphs)))
    if not candidates:
        return ""
    text = max(candidates, key=lambda v: len(v.split()))
    if len(text.split()) < 40:
        return ""
    bounded = text[:max_chars].rstrip()
    if len(text) > max_chars and " " in bounded:
        bounded = bounded.rsplit(" ", 1)[0]
    return bounded


def page_title(html: str) -> str:
    tree = HTMLParser(html)
    for selector in ('meta[property="og:title"]', "title", "h1"):
        node = tree.css_first(selector)
        if node is None:
            continue
        value = node.attributes.get("content") if selector.startswith("meta") else node.text(strip=True)
        if value:
            return " ".join(str(value).split())
    return ""


def fetch_html(url: str, *, timeout: float = 20.0) -> str:
    """GET one page; returns "" on non-200, non-HTML, or bot-wall responses."""
    headers = {
        "User-Agent": get_settings().http_user_agent,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
            response = client.get(url)
    except httpx.HTTPError as exc:
        logger.info("fetch failed url=%s error=%s", url, type(exc).__name__)
        return ""
    content_type = response.headers.get("content-type", "")
    if response.status_code != 200 or ("html" not in content_type and "xml" not in content_type):
        return ""
    return "" if fetch_blocked(response.text) else response.text


def fetch_text(url: str, *, timeout: float = 20.0) -> str:
    """GET a text resource (sitemap, JSON) without HTML filtering; "" on failure."""
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": get_settings().http_user_agent}
        ) as client:
            response = client.get(url)
    except httpx.HTTPError:
        return ""
    return response.text if response.status_code == 200 else ""


def fetch_source_excerpt(url: str, *, max_chars: int = 5000) -> tuple[str, str]:
    """Return (title, excerpt) for one source URL; both empty when the page cannot be read."""
    html = fetch_html(url)
    if not html:
        return "", ""
    return page_title(html), extract_source_excerpt(html, max_chars=max_chars)
