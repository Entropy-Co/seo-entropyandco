# Entropy & Co. SEO engine

Agentic SEO pipeline for [entropyand.co](https://entropyand.co): it finds the next article worth writing,
researches it from fetched sources, writes it, has an expert panel review it, and saves it to the Framer CMS.

It is a port of the BallersBank SEO engine (`../seo-ballersbank`) retargeted at Entropy's audience:
agency owners and operators of $1M to $50M businesses deciding where AI agents and automation pay off.
The Shopify, catalog, and sports-card parts are gone; Framer publishing, B2B topic strategy, and a
provider-agnostic LLM layer (Claude or OpenAI) are new.

## Pipeline

```
guards (velocity caps)
  └─ calendar: reserve next queued row ── queue empty ──► plan:
                                                          corpus (129+ live posts) → HN trends →
                                                          keyword research → strategy → enqueue
  └─ research   fetch approved authority pages + Entropy service pages → brief
                (LLM-proposed stats/facts kept only if the cited excerpt contains them)
  └─ write      Claude Opus / OpenAI heavy tier; deterministic format + humanization gates;
                failed checks become measured feedback and the draft is revised in place
  └─ panel      4 roles (SEO, E-E-A-T, humanization, conversion); weights and thresholds in code;
                iterate up to 4 rounds within the per-run cost cap
  └─ persist    drafts/<date>-<slug>/ (article.md, body.md, article.html, brief.json, report.json)
  └─ publish    only if SEO_PUBLISH_ENABLED=true and DRY_RUN=false: Framer draft / preview / live
  └─ finalize   calendar row + ledger + cost summary
```

| BallersBank | Entropy engine |
|---|---|
| Shopify blog publish | Framer CMS via `framer-bridge/` (official `framer-api`, Node) |
| Google Sheet calendar | `state/calendar.json` (atomic writes; same reserve/finalize lifecycle) |
| LangChain `ChatAnthropic` | `llm.py`: official `anthropic` and `openai` SDKs, per-tier provider |
| Temperature per draft/revision | `effort` per tier (Claude 5.x rejects sampling params) |
| last30days (Reddit/HN/YouTube via BrightData) | Hacker News Algolia API (free, no proxy) |
| Product links + commerce CTA | Service-page + related-article links, one restrained contact CTA |
| Collector vocabulary in dedupe | B2B/AI-ops vocabulary; dedupe against live sitemap + Framer + ledger |

## Setup

```bash
uv sync
(cd framer-bridge && pnpm install)
cp env.example .env          # add ANTHROPIC_API_KEY (or OPENAI_API_KEY + OPENAI_MODEL)
uv run entropyseo check      # config doctor, no paid calls
```

Optional signals: `SERPAPI_KEY` (related searches, People Also Ask, SERP sources) and
`GOOGLE_SERVICE_ACCOUNT_JSON` with Search Console read access (near-rank queries). Without them,
keyword research runs on the seed keywords in `config/niche.yaml` and research uses `seed_sources`.

### Connect Framer

1. In the Framer project, Site Settings > General: create an API key. Copy the project URL from the editor.
2. Set `FRAMER_PROJECT_URL` and `FRAMER_API_KEY` in `.env`.
3. `uv run entropyseo framer-check` lists every collection and field and validates `config/framer.yaml`.
   The field names there are placeholders until this passes.
4. Keep `FRAMER_PUBLISH_MODE=draft` until a few articles have been reviewed in Framer.

### Structured data

Each article's Article JSON-LD is built and checked in code (`tools/structured_data.py`) and written to the
`Schema JSON-LD` CMS field, in the same shape as the existing records. A headline that does not match the title,
a wrong URL, or a bad date stops the Framer write. `report.json` carries the same JSON-LD as `json_ld_preview`.

The field only reaches crawlers if the blog detail template renders it inside a
`<script type="application/ld+json">`. Until then, `schema-check` reports "no Article JSON-LD in the server HTML".

`preview` and `live` call Framer's `publish()`, which ships every pending change in the project. The bridge
refuses when anything outside `/blog/` is pending, so it can never publish someone else's half-done edits.

## Commands

```bash
uv run entropyseo corpus --refresh          # what already exists (dedupe + internal links)
uv run entropyseo keywords --limit 30       # scored candidates (no LLM)
uv run entropyseo plan --count 3            # queue topics (add --no-llm for a free deterministic pick)
uv run entropyseo calendar                  # view queue; --requeue <id> to retry a row
uv run entropyseo brief --title "..." --keyword "..."
uv run entropyseo write --brief drafts/brief.json
uv run entropyseo blog                      # one full scheduled run (the cron entrypoint)
uv run entropyseo framer-publish drafts/2026-10-10-some-slug --mode draft
uv run entropyseo sources-check             # verify seed_sources are fetchable
uv run entropyseo schema-check <slug|url>   # Article JSON-LD in the live server HTML (no JS)
```

## Configuration

- `config/niche.yaml`: brand positioning (from the pitch deck and site), pillars mapped to service pages,
  seed keywords, personas, internal pages, approved citation domains, prohibited claims, voice rules.
  Pitch-deck proof claims (partner networks, co-built products) are `verified: false` and never reach a
  prompt until someone confirms they are public and approved.
- `config/framer.yaml`: logical article fields mapped to exact Framer field names.
- `.env`: providers, models, effort, keys, safety switches. See `env.example`.

## Safety defaults

`DRY_RUN=true` and `SEO_PUBLISH_ENABLED=false`: runs write local drafts only. Other guards: velocity caps
(1/day, 3/week), per-run cost cap ($10), evidence hold when no source text could be fetched, deterministic
format failures hold without a panel call, duplicate-slug refusal in Framer, and refusal to publish
unrelated pending Framer changes. A shipped panel verdict means "ready for human review", not "publish".

## Deployment

`Dockerfile` + `railway.toml` run `entropyseo blog` on a Mon/Wed/Fri cron. Mount a volume at `/app/state`
so the calendar and ledger survive between runs. Nothing has been deployed yet.

## Development

```bash
uv run ruff check src tests && uv run mypy src && uv run pytest
```

## Not yet verified

- No live LLM run: no API key is configured in this checkout. Tests mock the model boundary.
- Framer is connected and `config/framer.yaml` validates, but no article has been written to the CMS yet.
- The blog template does not render the `Schema JSON-LD` field yet (see Structured data).
- No cover image source: `default_cover_image_url` is empty, so new articles have no Image and no JSON-LD `image`.
- SerpAPI and Search Console paths are untested against live accounts.
- Prompt quality is unproven until a few real articles have been reviewed.
