# AI & Robotics News Curation Bot

A **self-hosted, fully automated Telegram bot** that curates and delivers high-quality AI and Robotics news to the [AI and Robotics News](https://t.me/) Telegram Channel.

**Architecture:** Prefect 3 for workflow orchestration, PostgreSQL + pgvector for data and embeddings, OpenRouter for LLM curation, newsdata.io for ingestion.

---

## Project Structure

```
ai_robotics_news_bot/
├── docker-compose.yml            # Unified: profiles [server] + [worker]
├── Dockerfile                    # Worker image
├── .env                          # All secrets (gitignored)
├── .gitignore
│
├── config/                       # Declarative configuration (YAML)
│   ├── sources_whitelist.yml     # Allowed newsdata.io sources grouped by rotation set
│   ├── settings.yml              # Tunables: intervals, thresholds, model names
│   └── prompts/                  # LLM prompt templates
│       ├── curation.md
│       ├── categorization.md
│       └── Analysis.md           # Weekly report narrative prompt
│
├── db/
│   ├── init/
│   │   └── 01-create-databases.sql   # Creates newsbot DB + pgvector extension
│   └── schema.sql                    # Current schema reference (manually maintained)
│
├── src/                          # Application logic
│   ├── __init__.py
│   ├── config.py                 # Loads config/*.yml + env vars
│   ├── db.py                     # DB connection and queries
│   ├── ingestion.py              # newsdata.io fetcher
│   ├── curation.py               # LLM logic: categorization and curation (OpenRouter)
│   ├── embedding.py              # Title embedding via OpenRouter embeddings API
│   ├── analysis.py               # Weekly report: embedding clustering + LLM narrative
│   └── publishing.py             # Telegram message formatting and posting
│
├── flows/                        # Prefect flow definitions
│   ├── daily_news_flow.py        # Main production flow (7 tasks)
│   ├── report_flow.py            # Weekly report + cleanup flow (5 tasks)
│   └── start_flows.py            # Bootstrap: validates config and registers deployments
│
└── data/                         # Runtime data (gitignored)
    ├── last_news.json             # Ingestion state: last active group + collected articles
    └── to_publish.json            # Curation output: articles selected for current session
```

---

## API Budget & Ingestion Strategy

The ingestion pipeline runs on the **Newsdata.io Free Tier** (200 credits/day, 30 req/15 min):
* **7 Sessions per Day:** Orchestrated by Prefect, running approximately every 3.5 hours.
* **30 Requests per Session:** The bot randomly samples 5 domains from the whitelist and runs 6 broad topic queries against them (5 × 6 = 30 credits), filling the rate-limit window exactly.
* **State Management:** Fetch state is saved in `data/last_news.json` between runs so the pipeline resumes without re-fetching duplicate pages.
* **Database Deduplication:** Handled via PostgreSQL `UNIQUE` constraints and `ON CONFLICT` skips.

## Daily News Flow — 7 Tasks

Each Prefect run executes the following tasks sequentially:

| # | Task | What it does |
|---|------|--------------|
| 1 | `task1-load-config` | Loads and validates `settings.yml`, `sources_whitelist.yml`, and all env vars |
| 2 | `task2-run-ingestion` | Fetches articles from newsdata.io; rotates domain group; saves `last_news.json` |
| 3 | `task3-insert-to-db` | Inserts new articles into `newsbot` DB; skips duplicates via `ON CONFLICT` |
| 4 | `task4-categorize-backlog` | Sends uncategorized titles to OpenRouter in batches; writes `category` column |
| 5 | `task5-curate-articles` | Asks OpenRouter to select the best articles for this session; writes `to_publish.json` |
| 6 | `task6-publish-to-telegram` | Posts each selected article to the Telegram channel; marks `publicated=TRUE` in DB |
| 7 | `task7-embed-published` | Embeds titles of all published articles that lack a vector; writes 1536-dim vectors to `articles.embedding` (pgvector) |

**Duplicate publication protection:** Task 6 is gated on Task 5's in-session result — a leftover `to_publish.json` from a previous run is never used. After each successful post, the article is immediately marked `publicated=TRUE` so Task 5 never selects it again.

**Embedding backlog:** Task 7 runs after Task 6 and processes all `publicated=TRUE` rows that still have `embedding IS NULL` (up to `llm.embedding.batch_size` per run), so articles published in the current session are included on the same run.

---

## Weekly Report Flow — 5 Tasks

A **separate Prefect deployment** (`report-flow/weekly`) runs once per week and performs two responsibilities:

1. **DB Cleanup** — deletes unpublished articles older than `orchestration.report_interval_days` days, reclaiming storage used by articles that were ingested but never selected for publication.
2. **Trends Report** — clusters the embedding vectors of all published articles from the last `report_interval_days` days, then calls an LLM to generate a concise narrative digest, which is posted to the Telegram channel.

| # | Task | What it does |
|---|------|--------------|
| 1 | `report-task1-load-config` | Loads and validates `settings.yml` and all env vars |
| 2 | `report-task2-cleanup-db` | DELETEs `publicated=FALSE` rows older than N days; logs deleted count |
| 3 | `report-task3-fetch-embeddings` | Fetches published articles with non-null embedding vectors for the last N days |
| 4 | `report-task4-generate-report` | Clusters vectors (KMeans/MiniBatchKMeans), builds trend payload, calls `llm.analysis_model` to produce report text |
| 5 | `report-task5-post-to-telegram` | Formats HTML message and posts the report to the Telegram channel |

**Flow parameters** (configurable from Prefect UI or CLI):

| Parameter | Default | Effect |
|-----------|---------|--------|
| `skip_db_clean` | `False` | Skip Task 2 (run report only) |
| `skip_report` | `False` | Skip Tasks 3–5 (run cleanup only) |

**Clustering algorithm:** L2-normalised KMeans (≤ 500 articles) or MiniBatchKMeans (> 500 articles) from scikit-learn. Each cluster's representative titles are the articles geometrically closest to the centroid. Clusters smaller than `report.min_cluster_size` are filtered out before the LLM call.

**Prompt template:** `config/prompts/Analysis.md` — rendered with cluster JSON, article count, lookback days, and optional top-sources block.

## LLM Models (OpenRouter)

All models are routed through [OpenRouter](https://openrouter.ai/). The bot uses four models, one per role:

| Task | Model | Role |
|------|-------|------|
| Task 4 — Categorization | `meta-llama/llama-3.1-8b-instruct` | Fast batch labeling: assigns taxonomy categories (`"AI Policy"`, `"Humanoid Robots"`, …) to raw titles. High call volume. |
| Task 5 — Curation | `google/gemma-4-31b-it` | Selects the best articles from the categorized backlog for publication via tool-calling; uses recently published articles as diversity context. |
| Task 7 — Embedding | `qwen/qwen3-embedding-8b` | Converts published article titles to 1536-dim float vectors (pgvector). Native 4096-dim output is truncated to 1536 and L2-normalized. Dimension configurable via `llm.embedding.dimensions`; must match `VECTOR(N)` in `db/schema.sql`. |
| Report Task 4 — Analysis | `deepseek/deepseek-v4-flash` | Receives the clustered trend payload and writes a Telegram-ready weekly narrative digest. |

All model names are set in `config/settings.yml` (`llm.*_model` keys) and can be swapped without code changes.

## Data Flow

```
 .env                    config/settings.yml          config/sources_whitelist.yml
  │ secrets                │ tuning knobs                │ domain groups A/B/C…
  │                        │                             │
  └──────────┬─────────────┘                             │
             ▼                                           │
       ┌─────────────┐                                   │
       │  task1      │ ◄─────────────────────────────────┘
       │ load-config │  validates all config, fails fast
       └──────┬──────┘
              │ settings_obj, whitelist_obj
              ▼
       ┌─────────────┐       newsdata.io API
       │  task2      │ ◄──────────────────────
       │  ingestion  │  fetches up to 30 articles/domain group
       └──────┬──────┘
              │ articles[], collected_dt         data/last_news.json
              │                                  (rotation state, persisted)
              ▼
       ┌─────────────┐
       │  task3      │
       │ insert-to-db│  ON CONFLICT DO NOTHING → dedup
       └──────┬──────┘
              │
              ▼
     PostgreSQL newsbot DB
     articles (category=NULL, publicated=FALSE)
              │
              ▼
       ┌─────────────┐       OpenRouter  (categorization_model)
       │  task4      │ ◄──────────────────────────────────────
       │ categorize  │  batch titles → JSON array of labels
       │  backlog    │  loops until backlog empty or max_rounds
       └──────┬──────┘
              │ UPDATE articles SET category=…
              ▼
     articles (category≠NULL, publicated=FALSE)
              │
              ├─────────────────────────────────────────────────────────┐
              │ candidates (up to batch_size)    recently published      │
              │                                  (rag_context_size rows) │
              ▼                                                          │
       ┌─────────────┐       OpenRouter  (curation_model)               │
       │  task5      │ ◄──────────────────────────────────  ◄───────────┘
       │   curate    │  selects up to max_selected IDs
       └──────┬──────┘
              │                          data/to_publish.json
              │ selected ids + metadata  (written this session)
              ▼
       ┌─────────────┐       Telegram Bot API
       │  task6      │ ──────────────────────►  channel post
       │   publish   │  one HTTP POST per article
       └──────┬──────┘
              │ mark_publicated=TRUE (immediately after each post)
              ▼
     articles (publicated=TRUE)  →  excluded from future task5 candidates
              │
              ▼
       ┌─────────────┐       OpenRouter  (embedding_model)
       │  task7      │ ◄──────────────────────────────────
       │    embed    │  POST /embeddings  → VECTOR(1536)
       │  published  │  batch up to embedding.batch_size rows
       └──────┬──────┘
              │ UPDATE articles SET embedding=…
              ▼
     articles (embedding≠NULL)  →  ready for vector similarity search

```

**Key config knobs** (all in `config/settings.yml`):

| Setting | Controls |
|---|---|
| `orchestration.runs_per_day` | Prefect daily ingestion schedule |
| `orchestration.report_interval_days` | Cleanup age threshold + report lookback window (days) |
| `session.credits` | Max newsdata.io requests per run |
| `session.domains_per_session` | Domains sampled per run |
| `llm.timeout` | OpenRouter HTTP timeout (seconds), shared by all LLM tasks |
| `llm.categorization.batch_size` | Titles sent to LLM per round in task 4 |
| `llm.categorization.max_total_rounds` | Hard loop cap for task 4 |
| `llm.categorization.poison_mode` | `"mark"` silently labels bad titles; `"fail"` halts the flow |
| `llm.curation.batch_size` | Max candidate articles fetched for task 5 |
| `llm.curation.rag_context_size` | Recently published articles used as diversity context |
| `llm.curation.max_selected` | Max articles posted per session |
| `llm.embedding.dimensions` | Vector size — must match `VECTOR(N)` in `db/schema.sql` (default: 1536) |
| `llm.embedding.batch_size` | Max published articles embedded per run in task 7 |
| `llm.analysis_model` | OpenRouter model for weekly report narrative (report task 4) |
| `llm.analysis.temperature` | Sampling temperature for report LLM call |
| `llm.analysis.timeout` | HTTP timeout override for report LLM call (seconds) |
| `llm.analysis.max_output_chars` | Hard character cap applied in Python before posting |
| `report.max_articles_to_analyze` | Max published articles fetched for clustering |
| `report.max_clusters` | KMeans k (upper bound; actual clusters may be fewer) |
| `report.min_cluster_size` | Clusters with fewer articles than this are excluded |
| `report.max_titles_per_cluster` | Representative titles per cluster shown in LLM prompt |
| `report.max_message_chars` | Telegram HTML message length limit (safety trim) |
| `report.include_sources_summary` | Append top-5 source domains to LLM prompt |

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/lexmaister/ai_robotics_news_bot.git
cd ai_robotics_news_bot
# Create .env from the template in the Environment Variables section below
# and fill in your actual secrets
```

### 2. Start the server infrastructure

```bash
docker compose --profile server up -d
```

This starts: PostgreSQL (with pgvector), Redis, Prefect API server and Prefect background services.

### 3. Verify Prefect Server is running

```bash
# Health check
curl -s http://localhost:4200/api/health && echo ""
# → true
```

### 4. Accessing Prefect UI via SSH Tunnel

If your Prefect server is running on a remote machine, you can access the dashboard locally through an SSH tunnel.

#### Prerequisites
- SSH access to the remote server (password or ssh-key)
- Prefect server container running with these environment variables:
    PREFECT_SERVER_API_HOST: 0.0.0.0
    PREFECT_UI_API_URL: http://127.0.0.1:4200/api

#### Setting up the tunnel

Using terminal:

```
ssh -L 4200:127.0.0.1:4200 user@your-server-ip
```

#### Access the dashboard

Once the tunnel is active, open in your browser:

```
http://127.0.0.1:4200/dashboard
```

#### Notes
- PREFECT_SERVER_API_HOST: 0.0.0.0 ensures the server listens on all interfaces
  inside the container, allowing Docker port mapping to work.
- PREFECT_UI_API_URL: http://127.0.0.1:4200/api tells the browser-based UI
  to call the API at your local tunneled address instead of the default 0.0.0.0.

### 5. Start the worker

```bash
docker compose --profile worker up -d

# View worker logs
docker compose logs -f worker
```

### 6. Tear down

```bash
# Stop everything
docker compose --profile server --profile worker down

# Stop + delete volumes (loses all data)
docker compose --profile server --profile worker down -v
```

### 5a. (Optional) Start worker with external network for outbound traffic

When outbound API calls must be routed through a specific network interface,
the worker can join a pre-existing Docker network (`shared_vpn`) **in addition
to** the default compose network it uses for internal communication with
`prefect-server` and `postgres`.

```bash
# Create the shared network once on the host (skip if it already exists)
docker network create shared_vpn

# Start worker attached to both the compose default network and shared_vpn
docker compose -f docker-compose.yml -f docker-compose.vpn.yml --profile worker up -d
```

---

## Architecture Decisions

| Topic | Decision | Rationale |
|-------|----------|-----------|
| Networking | Compose default network | One fewer setup step, fully declarative |
| Compose structure | Single file with profiles `[server]` + `[worker]` | Zero manual steps, clear separation |
| Database | PostgreSQL 16 + pgvector | One server, two DBs: `prefect` + `newsbot` |
| Vector search | pgvector in `newsbot` DB | Hybrid SQL + vector queries, no extra service |
| Branching | Single `main` branch + git tags | Solo maintainer, no coordination overhead |
| Secrets | Single `.env` at project root | Compose reads automatically, gitignored |
| Schema management | Manual `schema.sql` + init script | Minimal; add Alembic when data is valuable |
| Config | YAML in `config/`, mounted read-only | Editable without code changes or rebuilds |

---

## Environment Variables

All secrets and configuration live in a single `.env` file (never committed):

```bash
# Infrastructure
POSTGRES_PASSWORD=your_secure_password

# External APIs
NEWSDATA_API_KEY=pub_xxxxxxxxxxxxx
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxx

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC-xxxxxxxxxxxxx
TELEGRAM_CHANNEL_ID=-1001234567890
```

---

## Database Architecture

Single PostgreSQL instance (`pgvector/pgvector:pg16`), two databases:

- **`prefect`** — Prefect internal state (flows, runs, schedules)
- **`newsbot`** — Application data (articles, embeddings, metadata, analytics)

The `db/init/01-create-databases.sql` script creates both on first start. See `db/schema.sql` for the current `newsbot` schema.

---

## Development

Volumes mount `src/`, `flows/`, and `config/` into the worker container for live editing:

```yaml
volumes:
  - ./config:/app/config:ro
  - ./src:/app/src
  - ./flows:/app/flows
```

For production: remove dev mounts, bake code into the Docker image, tag with git version.

---

## Useful Commands

```bash
# Start server only
docker compose --profile server up -d

# Start worker only (server must be running)
docker compose --profile worker up -d

# Restart worker after code changes
docker compose --profile worker restart

# Run a flow manually
docker exec -it news_worker python -m flows.daily_news_flow

# Run the weekly report flow manually
docker exec -it news_worker python -m flows.report_flow

# Run cleanup only (skip report posting)
docker exec -it news_worker python -c "from flows.report_flow import report_flow; report_flow(skip_report=True)"

# Register/update ALL Prefect deployments (daily + weekly)
docker exec -it news_worker python -m flows.start_flows

# Check Prefect API health
curl -s http://localhost:4200/api/health

# View logs
docker compose logs -f worker
docker compose logs -f prefect-server
```

### Database Insights & Statistics

Use these quick commands to check the health and progress of your ingestion and curation pipelines without leaving the terminal.

**Check Overall Pipeline Progress**
See how many articles are pending publication versus already published:
```bash
docker exec -it ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot -c "SELECT publicated as is_published, COUNT(*) as article_count FROM articles GROUP BY publicated;"
```

**Check Categorization Statistics**
View the distribution of articles across AI and Robotics categories, including how many are currently sitting in the backlog as `Uncategorized` (NULL):
```bash
docker exec -it ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot -c "SELECT COALESCE(category, 'Uncategorized (Pending)') as category_name, COUNT(*) as total FROM articles GROUP BY category ORDER BY total DESC;"
```

**Monitor Today's Ingestion Volume**
Verify how many new articles the Newsdata.io fetcher has successfully ingested in the last 24 hours:
```bash
docker exec -it ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot -c "SELECT COUNT(*) as ingested_last_24h FROM articles WHERE collected_dt >= NOW() - INTERVAL '1 day';"
```

**View the 3 Most Recently Published Articles**
Quickly check the latest articles that were sent to the Telegram channel:
```bash
docker exec -it ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot -c "SELECT title, pub_date FROM articles WHERE publicated = TRUE ORDER BY pub_date DESC LIMIT 3;"
```

**Check Embedding Coverage**
See how many published articles have been embedded versus still awaiting a vector:
```bash
docker exec -it ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot -c "SELECT (embedding IS NOT NULL) as has_embedding, COUNT(*) FROM articles WHERE publicated = TRUE GROUP BY has_embedding;"
```
---

## Persistence and Backups

PostgreSQL data is stored in a Docker named volume (`postgres_data`). If the host OS is reinstalled, volumes are lost.

Recommended periodic backups:

```bash
docker exec ai_robotics_news_bot-postgres-1 pg_dump -U prefect newsbot > backup_newsbot.sql
docker exec ai_robotics_news_bot-postgres-1 pg_dump -U prefect prefect > backup_prefect.sql
```

---

## License

MIT
