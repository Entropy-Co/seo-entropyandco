"""Framer CMS client: drives framer-bridge/bridge.mjs over JSON stdin/stdout.

Publishing modes (FRAMER_PUBLISH_MODE):
- draft:   create the CMS item with draft=true. Nothing goes live; an editor reviews in Framer.
- preview: create a non-draft item and publish a preview deployment. Production is untouched.
- live:    preview, then promote that exact deployment to the custom domain.

`publish()` deploys every pending change in the project, so the bridge refuses
when anything outside the blog path is pending.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from entropyseo.config import get_settings
from entropyseo.runtime_logging import redact_sensitive_text

logger = logging.getLogger(__name__)


class FramerError(RuntimeError):
    """The bridge reported a failure, or Framer is not configured."""


class FramerFieldMap(BaseModel):
    """Logical article field -> exact Framer field name. None skips the field."""

    title: str = "Title"
    body: str = "Content"
    excerpt: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    published_date: str | None = None
    author: str | None = None
    category: str | None = None
    cover_image: str | None = None
    read_time: str | None = None
    updated_at: str | None = None
    topic_pillar: str | None = None
    target_keywords: str | None = None
    schema_json_ld: str | None = None


class SchemaConfig(BaseModel):
    """Constants for the Article JSON-LD stored in the CMS."""

    blog_name: str = ""
    language: str = "en-US"
    max_about_terms: int = Field(default=8, ge=0)


class FramerConfig(BaseModel):
    collection: str = "Blog"
    fields: FramerFieldMap = Field(default_factory=FramerFieldMap)
    body_content_type: str = Field(default="html", pattern="^(html|markdown)$")
    default_category: str = ""
    default_cover_image_url: str = ""
    default_cover_image_alt: str = ""
    topic_pillars: dict[str, str] = Field(default_factory=dict)
    schema_: SchemaConfig = Field(default_factory=SchemaConfig, alias="schema")


@lru_cache(maxsize=1)
def load_framer_config() -> FramerConfig:
    path = Path(get_settings().framer_config_path)
    if not path.is_file():
        return FramerConfig()
    return FramerConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def pillar_label(pillar: str | None) -> str:
    """The Framer "Topic Pillar" id for a niche.yaml pillar name; unmapped names pass through."""
    if not pillar:
        return ""
    return load_framer_config().topic_pillars.get(pillar, pillar)


def framer_configured() -> bool:
    s = get_settings()
    return bool(s.framer_project_url and s.framer_api_key)


def _run(command: str, args: dict[str, Any] | None = None) -> Any:
    s = get_settings()
    if not framer_configured():
        raise FramerError("Framer is not configured: set FRAMER_PROJECT_URL and FRAMER_API_KEY")
    bridge_dir = Path(s.framer_bridge_dir).resolve()
    script = bridge_dir / "bridge.mjs"
    if not (bridge_dir / "node_modules" / "framer-api").exists():
        raise FramerError(f"framer-api is not installed; run `pnpm install` in {bridge_dir}")
    env = {**os.environ, "FRAMER_PROJECT_URL": s.framer_project_url, "FRAMER_API_KEY": s.framer_api_key}
    try:
        completed = subprocess.run(
            ["node", str(script), command],
            input=json.dumps(args or {}),
            capture_output=True,
            text=True,
            cwd=bridge_dir,
            env=env,
            timeout=s.framer_bridge_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FramerError(f"framer bridge `{command}` timed out after {s.framer_bridge_timeout_seconds}s") from exc
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        detail = redact_sensitive_text((completed.stderr or completed.stdout)[-800:])
        raise FramerError(f"framer bridge `{command}` returned non-JSON output: {detail}") from exc
    if not payload.get("ok"):
        raise FramerError(f"framer `{command}` failed: {redact_sensitive_text(str(payload.get('error')))}")
    return payload["result"]


def inspect_project() -> dict[str, Any]:
    return _run("inspect")


def list_items() -> list[dict[str, Any]]:
    return _run("list-items", {"collection": load_framer_config().collection})


def validate_mapping(inspection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check every mapped field name exists on the configured collection. Returns missing names per logical field."""
    config = load_framer_config()
    inspection = inspection or inspect_project()
    collection = next(
        (c for c in inspection.get("collections", []) if config.collection in (c.get("name"), c.get("id"))), None
    )
    if collection is None:
        return {
            "collection_found": False,
            "missing": {},
            "available": [c.get("name") for c in inspection.get("collections", [])],
        }
    names = {f["name"]: f["type"] for f in collection.get("fields", [])}
    mapped = {k: v for k, v in config.fields.model_dump().items() if v}
    missing = {logical: name for logical, name in mapped.items() if name not in names}
    return {
        "collection_found": True,
        "collection": collection.get("name"),
        "managed_by": collection.get("managedBy"),
        "missing": missing,
        "mapped": {logical: {"field": name, "type": names.get(name)} for logical, name in mapped.items()},
        "available": sorted(names),
    }


def article_values(
    *,
    title: str,
    body_html: str,
    body_markdown: str,
    excerpt: str,
    meta_title: str,
    meta_description: str,
    published_date: str,
    read_time_minutes: int,
    updated_at: str = "",
    topic_pillar: str = "",
    target_keywords: str = "",
    schema_json_ld: str = "",
    cover_image_url: str = "",
    cover_image_alt: str = "",
) -> dict[str, dict[str, Any]]:
    """Build the bridge `values` payload keyed by real Framer field names."""
    config = load_framer_config()
    f = config.fields
    body = body_markdown if config.body_content_type == "markdown" else body_html
    values: dict[str, dict[str, Any]] = {
        f.title: {"value": title},
        f.body: {"value": body, "contentType": config.body_content_type},
    }
    optional: list[tuple[str | None, dict[str, Any]]] = [
        (f.excerpt, {"value": excerpt}),
        (f.meta_title, {"value": meta_title}),
        (f.meta_description, {"value": meta_description}),
        (f.published_date, {"value": published_date}),
        (f.author, {"value": get_settings().blog_author}),
        (f.read_time, {"value": read_time_minutes}),
    ]
    optional += [
        (name, {"value": value})
        for name, value in (
            (f.updated_at, updated_at),
            (f.topic_pillar, topic_pillar),
            (f.target_keywords, target_keywords),
            (f.schema_json_ld, schema_json_ld),
        )
        if value
    ]
    if config.default_category:
        optional.append((f.category, {"value": config.default_category}))
    image_url = cover_image_url or config.default_cover_image_url
    if image_url:
        optional.append(
            (f.cover_image, {"value": image_url, "alt": cover_image_alt or config.default_cover_image_alt or title})
        )
    for name, entry in optional:
        if name:
            values[name] = entry
    return values


def upsert_item(*, slug: str, values: dict[str, dict[str, Any]], draft: bool) -> dict[str, Any]:
    return _run(
        "upsert-item",
        {"collection": load_framer_config().collection, "slug": slug, "draft": draft, "values": values},
    )


def publish_preview(*, allow_unrelated: bool = False) -> dict[str, Any]:
    return _run("publish", {"pathPrefix": get_settings().blog_path_prefix, "allowUnrelated": allow_unrelated})


def deploy(deployment_id: str) -> dict[str, Any]:
    return _run("deploy", {"deploymentId": deployment_id})
