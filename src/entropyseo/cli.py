"""entropyseo command line.

entropyseo check               config doctor (providers, models, keys, niche, Framer mapping file)
entropyseo corpus              inventory existing articles (sitemap + blog index + Framer + ledger)
entropyseo keywords            scored keyword candidates from seeds, SerpAPI, and GSC
entropyseo plan                pick topics with strategy and queue them on the calendar
entropyseo calendar            show the calendar queue
entropyseo brief               build a research brief for a title + keyword
entropyseo write               write + panel-review one brief (no calendar, no publish)
entropyseo blog                one scheduled run: calendar -> research -> write -> panel -> persist -> publish gate
entropyseo framer-check        list Framer collections/fields and validate config/framer.yaml
entropyseo framer-publish      push a reviewed draft folder to Framer
entropyseo sources-check       confirm configured seed sources are fetchable
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from entropyseo.config import get_settings, load_niche_config
from entropyseo.runtime_logging import configure_logging

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Entropy & Co. agentic SEO engine")
console = Console()


@app.callback()
def _main(log_level: str = typer.Option("", help="Override LOG_LEVEL")) -> None:
    configure_logging(log_level or None)


def _print_json(value: Any) -> None:
    console.print_json(json.dumps(value, default=str, ensure_ascii=False))


@app.command()
def check() -> None:
    """Validate configuration without calling any paid API."""
    s = get_settings()
    niche = load_niche_config()
    table = Table(title="LLM tiers")
    for col in ("tier", "provider", "model", "effort", "key"):
        table.add_column(col)
    problems: list[str] = []
    for tier in ("default", "heavy", "fast", "judge"):
        provider = s.provider_for(tier)  # type: ignore[arg-type]
        model = s.model_for(tier, strict=False)  # type: ignore[arg-type]
        key = s.anthropic_api_key if provider == "anthropic" else s.openai_api_key
        table.add_row(
            tier, provider, model or "[red]unset[/red]", s.effort_for(tier), "set" if key else "[red]missing[/red]"
        )  # type: ignore[arg-type]
        if not model:
            problems.append(f"{tier}: no model configured for {provider}")
        if not key:
            problems.append(f"{tier}: {provider} API key missing")
    console.print(table)
    if s.judge_matches_writer():
        console.print(
            "[yellow]Warning: judge and writer use the same model; the panel will grade its own drafts.[/yellow]"
        )
    from entropyseo.tools import framer_tools, search_tools

    console.print(
        f"Niche: {niche.brand_entity.name} · {len(niche.pillars)} pillars · {len(niche.priority_keywords)} seed keywords"
    )
    unverified = [c.claim for c in niche.brand_entity.proof_claims if not c.verified]
    if unverified:
        console.print(f"[yellow]{len(unverified)} proof claims unverified (kept out of prompts).[/yellow]")
    console.print(f"SerpAPI: {'on' if search_tools.serpapi_available() else 'off (niche seeds + seed_sources only)'}")
    console.print(f"Search Console: {'on' if s.google_service_account_json else 'off'}")
    console.print(
        f"Framer: {'configured' if framer_tools.framer_configured() else 'not configured'} · mode={s.framer_publish_mode}"
    )
    console.print(
        f"Safety: dry_run={s.dry_run} publish_enabled={s.seo_publish_enabled} cost_cap=${s.cost_cap_per_run_usd} "
        f"velocity={s.velocity_cap_per_day}/day {s.velocity_cap_per_week}/week"
    )
    if problems:
        console.print("[red]Problems:[/red]")
        for p in problems:
            console.print(f"  - {p}")
        raise typer.Exit(1)


@app.command()
def corpus(refresh: bool = typer.Option(False, help="Rebuild instead of using the 24h cache")) -> None:
    """Inventory existing articles used for dedupe and internal links."""
    from entropyseo.tools import corpus_tools

    posts = corpus_tools.load_corpus(refresh=refresh)
    by_source: dict[str, int] = {}
    for p in posts:
        by_source[p.source] = by_source.get(p.source, 0) + 1
    console.print(f"{len(posts)} articles · {by_source}")
    for p in posts[:15]:
        console.print(f"  {p.slug} · {p.title}")


@app.command()
def keywords(seed: str = typer.Option("", help="Optional seed topic"), limit: int = 30, clusters: bool = False) -> None:
    """Score keyword candidates (no LLM unless --clusters)."""
    from entropyseo.agents import keyword_research

    result = keyword_research.run(seed or None, with_clusters=clusters)
    table = Table(title=f"Top {limit} candidates · signals {result['signals']}")
    for col in ("score", "term", "intent", "pillar", "sources"):
        table.add_column(col)
    for c in result["candidates"][:limit]:
        table.add_row(f"{c['score']:.2f}", c["term"], c["intent"], c["pillar"] or "-", ",".join(c["sources"]))
    console.print(table)
    if clusters:
        _print_json({"strategic_headline": result["strategic_headline"], "clusters": result["clusters"]})


@app.command()
def plan(
    count: int = typer.Option(1, min=1, max=10), no_llm: bool = typer.Option(False, help="Deterministic pick only")
) -> None:
    """Pick the next topic(s) and append them to the calendar."""
    from entropyseo.agents import content_strategy, keyword_research
    from entropyseo.chains import calendar
    from entropyseo.schemas import CalendarCandidate
    from entropyseo.tools import corpus_tools, search_tools

    niche = load_niche_config()
    existing = [p.title for p in corpus_tools.load_corpus() if p.title] + calendar.all_titles()
    trends = search_tools.hackernews_trends(niche.trend_queries[:6]) if niche.trend_queries else []
    research = keyword_research.run(with_clusters=not no_llm)
    for _ in range(count):
        decision = content_strategy.run(
            candidates=research["candidates"],
            existing_titles=existing,
            trends=trends,
            clusters=research["clusters"],
            use_llm=not no_llm,
        )
        if decision["decision"] != "proceed":
            console.print(f"[yellow]skip: {decision.get('skip_reason')}[/yellow]")
            break
        candidate = CalendarCandidate.model_validate(decision["calendar_candidate"])
        row = calendar.append(candidate)
        existing.append(candidate.title)
        console.print(f"queued [bold]{candidate.title}[/bold] · {candidate.primary_keyword} · row {row.id}")
        console.print(f"  {decision.get('rationale')}")


@app.command("calendar")
def calendar_cmd(requeue: str = typer.Option("", help="Row id to put back in the queue")) -> None:
    """Show the calendar queue."""
    from entropyseo.chains import calendar

    if requeue:
        row = calendar.requeue(requeue)
        console.print(f"requeued {row.title}" if row else f"[red]no row {requeue}[/red]")
        return
    table = Table(title="SEO calendar")
    for col in ("id", "status", "priority", "title", "keyword", "result"):
        table.add_column(col)
    for row in calendar.read_rows():
        result = row.result.get("skip_reason") or row.result.get("slug") or ""
        table.add_row(
            row.id,
            row.status,
            f"{row.candidate.priority_score:.1f}",
            row.title,
            row.candidate.primary_keyword,
            str(result),
        )
    console.print(table)


def _candidate(title: str, keyword: str, pillar: str) -> Any:
    from entropyseo.agents.keyword_research import assign_pillar, classify_intent
    from entropyseo.agents.topic_dedupe import topic_family_id
    from entropyseo.schemas import CalendarCandidate

    return CalendarCandidate(
        canonical_topic_id=topic_family_id(keyword),
        title=title,
        primary_keyword=keyword,
        intent=classify_intent(keyword),  # type: ignore[arg-type]
        pillar=pillar or assign_pillar(keyword),
        dedupe_family=topic_family_id(keyword),
        evidence=["manual brief"],
    )


@app.command()
def brief(
    title: str = typer.Option(..., help="Article title"),
    keyword: str = typer.Option(..., help="Primary keyword"),
    pillar: str = typer.Option("", help="Pillar name (auto-assigned when empty)"),
    output: Path = typer.Option(Path("drafts/brief.json")),
) -> None:
    """Build a research brief for one title (calls the default-tier model once)."""
    from entropyseo.agents import content_research_writer
    from entropyseo.llm import CostLedger, set_cost_ledger
    from entropyseo.tools import corpus_tools

    ledger = CostLedger()
    set_cost_ledger(ledger)
    result = content_research_writer.run(_candidate(title, keyword, pillar), corpus=corpus_tools.load_corpus())
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    console.print(
        f"brief → {output} · sources={len(result.source_context)} facts={len(result.verified_facts)} "
        f"links={len(result.links)} evidence_floor={result.evidence_floor_enforced} cost={ledger.summary()}"
    )


@app.command()
def write(
    brief_path: Path = typer.Option(..., "--brief", exists=True),
    rounds: int = typer.Option(0, help="Override panel rounds"),
) -> None:
    """Write and panel-review one brief; persists the draft locally. Never publishes."""
    from entropyseo.agents import content_ops, seo_blog_writer
    from entropyseo.llm import CostLedger, set_cost_ledger
    from entropyseo.schemas import ContentBrief
    from entropyseo.tools import drafts_tools

    ledger = CostLedger()
    set_cost_ledger(ledger)
    content_brief = ContentBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    max_rounds = rounds or get_settings().panel_max_rounds
    draft, panel = None, None
    for round_number in range(1, max_rounds + 1):
        draft = seo_blog_writer.run(content_brief, content_ops.feedback_for_revision(panel), draft_to_revise=draft)
        panel = content_ops.run(draft, content_brief, round_number=round_number)
        console.print(f"round {round_number}: {panel.status} composite={panel.composite}")
        if panel.status != "iterate" or ledger.usd >= get_settings().cost_cap_per_run_usd:
            break
    assert draft is not None and panel is not None
    status = "ship" if panel.status == "ship" else "hold"
    persisted = drafts_tools.persist_draft(
        draft=draft,
        brief=content_brief,
        status=status,
        report={"panel": panel.model_dump(mode="json"), "format_report": draft.format_report, "cost": ledger.summary()},
        run_date=datetime.now(UTC).date().isoformat(),
    )
    console.print(f"{status} → {persisted['folder']} · cost={ledger.summary()}")


@app.command()
def blog() -> None:
    """One scheduled run (the cron entrypoint)."""
    from entropyseo.chains.blog_graph import run_blog

    result = run_blog()
    _print_json(result)
    if result.get("status") == "failed":
        raise typer.Exit(1)


@app.command("framer-check")
def framer_check() -> None:
    """Show Framer collections and fields, and validate config/framer.yaml against them."""
    from entropyseo.tools import framer_tools

    inspection = framer_tools.inspect_project()
    console.print(f"Project: {inspection['project']['name']}")
    for collection in inspection["collections"]:
        console.print(f"[bold]{collection['name']}[/bold] ({collection['managedBy']}, {collection['itemCount']} items)")
        for field in collection["fields"]:
            console.print(f"   {field['name']} · {field['type']}")
    validation = framer_tools.validate_mapping(inspection)
    _print_json(validation)
    if not validation["collection_found"] or validation["missing"]:
        console.print("[red]config/framer.yaml does not match the project. Fix the names above.[/red]")
        raise typer.Exit(1)


@app.command("framer-publish")
def framer_publish(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, help="drafts/<date>-<slug> folder"),
    mode: str = typer.Option("draft", help="draft | preview | live"),
    allow_unrelated: bool = typer.Option(False, help="Publish even if non-blog changes are pending in Framer"),
) -> None:
    """Push a reviewed local draft to Framer."""
    from entropyseo.tools import drafts_tools, framer_tools, structured_data

    if mode not in {"draft", "preview", "live"}:
        raise typer.BadParameter("mode must be draft, preview, or live")
    text = (folder / "article.md").read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---", 2)[1])
    if meta.get("status") == "hold":
        raise typer.BadParameter("this draft is on hold; review and move it out of drafts/holds first")
    body_markdown = (folder / "body.md").read_text(encoding="utf-8")
    try:
        values = drafts_tools.framer_values(
            title=meta["title"],
            slug=meta["slug"],
            body_markdown=body_markdown,
            excerpt=meta.get("excerpt") or "",
            meta_title=meta["meta_title"],
            meta_description=meta["meta_description"],
            generated_at=datetime.fromisoformat(str(meta["generated_at"])),
            read_time_minutes=int(meta.get("read_time_minutes") or 1),
            primary_keyword=meta.get("primary_keyword") or "",
            keywords=list(meta.get("keywords") or []),
            pillar=meta.get("pillar"),
        )
    except structured_data.StructuredDataError as exc:
        raise typer.BadParameter(str(exc)) from exc
    item = framer_tools.upsert_item(slug=meta["slug"], values=values, draft=(mode == "draft"))
    result: dict[str, Any] = {"item": item}
    if mode in {"preview", "live"}:
        result["preview"] = framer_tools.publish_preview(allow_unrelated=allow_unrelated)
        if mode == "live":
            result["deploy"] = framer_tools.deploy(str(result["preview"]["deployment"]["id"]))
    drafts_tools.append_ledger({"slug": meta["slug"], "title": meta["title"], "status": f"framer-{mode}"})
    _print_json(result)


@app.command("sources-check")
def sources_check() -> None:
    """Fetch every configured seed source and report readable word counts."""
    from entropyseo.tools import web_tools

    urls = sorted({u for urls in load_niche_config().seed_sources.values() for u in urls})
    failed = 0
    for url in urls:
        title, excerpt = web_tools.fetch_source_excerpt(url)
        words = len(excerpt.split())
        failed += words < 40
        console.print(f"{'ok ' if words >= 40 else '[red]bad[/red]'} {words:5d} words · {url} · {title[:60]}")
    if failed:
        raise typer.Exit(1)


@app.command("schema-check")
def schema_check(
    targets: list[str] = typer.Argument(..., help="Article slugs or full URLs"),
) -> None:
    """Check the Article JSON-LD in each live page's server HTML (what a crawler sees without running JS)."""
    from entropyseo.tools import drafts_tools, structured_data, web_tools

    failed = 0
    for target in targets:
        url = target if target.startswith("http") else drafts_tools.article_url(target)
        html = web_tools.fetch_html(url)
        if html:
            report = structured_data.live_check(html, url=url)
        else:
            report = {"url": url, "ok": False, "problems": ["fetch failed or not HTML"], "types": [], "warnings": []}
        failed += not report["ok"]
        console.print(
            f"{'ok ' if report['ok'] else '[red]bad[/red]'} {url} · types: {', '.join(map(str, report['types']))}"
        )
        for line in report["problems"]:
            console.print(f"    [red]✗[/red] {line}")
        for line in report["warnings"]:
            console.print(f"    [yellow]![/yellow] {line}")
    if failed:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
