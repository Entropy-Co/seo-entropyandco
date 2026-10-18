"""Search-demand signals: SerpAPI expansion, Google Search Console, Hacker News trends.

Every source is optional. A missing key returns an empty result so the pipeline
degrades to niche seeds instead of crashing. Nothing here invents volume data.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any, TypedDict

import httpx

from entropyseo.config import get_settings
from entropyseo.schemas import TrendSignal

logger = logging.getLogger(__name__)

_SERPAPI = "https://serpapi.com/search.json"
_HN_SEARCH = "https://hn.algolia.com/api/v1/search"


class SerpResult(TypedDict):
    title: str
    url: str
    snippet: str
    position: int


class SerpExpansion(TypedDict):
    related_searches: list[str]
    people_also_ask: list[str]
    serp_features: list[str]
    top_results: list[SerpResult]


def _empty_expansion() -> SerpExpansion:
    return {"related_searches": [], "people_also_ask": [], "serp_features": [], "top_results": []}


def serpapi_available() -> bool:
    return bool(get_settings().serpapi_key)


def serpapi_expand(query: str) -> SerpExpansion:
    """Related searches, People Also Ask, SERP features, and top organic results for one query."""
    s = get_settings()
    if not s.serpapi_key:
        return _empty_expansion()
    try:
        with httpx.Client(timeout=30) as client:
            response = client.get(
                _SERPAPI,
                params={
                    "engine": "google",
                    "q": query,
                    "gl": s.serpapi_market,
                    "hl": "en",
                    "num": 10,
                    "api_key": s.serpapi_key,
                },
            )
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("serpapi query failed query=%s error=%s", query, type(exc).__name__)
        return _empty_expansion()
    features = [
        name
        for key, name in (
            ("ai_overview", "ai_overview"),
            ("answer_box", "answer_box"),
            ("related_questions", "people_also_ask"),
            ("top_stories", "top_stories"),
            ("inline_videos", "video"),
        )
        if data.get(key)
    ]
    return {
        "related_searches": [r["query"] for r in data.get("related_searches", []) if r.get("query")],
        "people_also_ask": [q["question"] for q in data.get("related_questions", []) if q.get("question")],
        "serp_features": features,
        "top_results": [
            {
                "title": str(r.get("title") or ""),
                "url": str(r.get("link") or ""),
                "snippet": str(r.get("snippet") or ""),
                "position": int(r.get("position") or 0),
            }
            for r in data.get("organic_results", [])[:10]
            if r.get("link")
        ],
    }


class GscQueryRow(TypedDict):
    query: str
    page: str
    impressions: int
    clicks: int
    position: float


@lru_cache(maxsize=1)
def _gsc_service() -> Any:
    import google_auth_httplib2
    import httplib2
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_service_account_info(
        get_settings().google_service_account_info(),
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=20))
    return build("searchconsole", "v1", http=http, cache_discovery=False)


def gsc_queries(*, days_back: int = 28, min_impressions: int = 20, row_limit: int = 1000) -> list[GscQueryRow]:
    """Top (query, page) rows from Search Console; [] when GSC is not configured or not permitted."""
    s = get_settings()
    if not s.google_service_account_json:
        return []
    end = date.today()
    body = {
        "startDate": (end - timedelta(days=days_back - 1)).isoformat(),
        "endDate": end.isoformat(),
        "dimensions": ["query", "page"],
        "rowLimit": row_limit,
    }
    try:
        response = _gsc_service().searchanalytics().query(siteUrl=s.gsc_site_url, body=body).execute()
    except Exception as exc:
        logger.warning("gsc query failed site=%s error=%s", s.gsc_site_url, exc)
        return []
    out: list[GscQueryRow] = []
    for row in response.get("rows", []) or []:
        impressions = int(row.get("impressions", 0))
        if impressions >= min_impressions:
            out.append(
                {
                    "query": str(row["keys"][0]),
                    "page": str(row["keys"][1]),
                    "impressions": impressions,
                    "clicks": int(row.get("clicks", 0)),
                    "position": float(row.get("position", 0.0)),
                }
            )
    return out


def gsc_near_rank(
    rows: list[GscQueryRow], *, min_position: float = 8.0, max_position: float = 25.0
) -> list[GscQueryRow]:
    """Queries ranking just off page one: the cheapest wins for a new or refreshed article."""
    return [r for r in rows if min_position <= r["position"] <= max_position]


def hackernews_trends(queries: list[str], *, days: int = 30, per_query: int = 5) -> list[TrendSignal]:
    """Recent high-engagement Hacker News stories for each query (free public API, no key)."""
    since = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
    signals: dict[str, TrendSignal] = {}
    with httpx.Client(timeout=20, headers={"User-Agent": get_settings().http_user_agent}) as client:
        for query in queries:
            try:
                response = client.get(
                    _HN_SEARCH,
                    params={
                        "query": query,
                        "tags": "story",
                        "numericFilters": f"created_at_i>{since},points>20",
                        "hitsPerPage": per_query,
                    },
                )
                response.raise_for_status()
                hits = response.json().get("hits", [])
            except (httpx.HTTPError, ValueError):
                continue
            for hit in hits:
                url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                if url in signals or not hit.get("title"):
                    continue
                signals[url] = TrendSignal(
                    title=str(hit["title"]),
                    url=url,
                    points=int(hit.get("points") or 0),
                    comments=int(hit.get("num_comments") or 0),
                    created_at=str(hit.get("created_at") or ""),
                )
    return sorted(signals.values(), key=lambda s: s.points + 2 * s.comments, reverse=True)
