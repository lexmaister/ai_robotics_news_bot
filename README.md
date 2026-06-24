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
│       └── categorization.md
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
│   └── publishing.py             # Telegram message formatting and posting
│
├── flows/                        # Prefect flow definitions
│   ├── daily_news_flow.py        # Main production flow (6 tasks)
│   └── start_flows.py            # Bootstrap: validates config and registers deployments
│
└── data/                         # Runtime data (gitignored)
    ├── last_news.json             # Ingestion state: last active group + collected articles
    └── to_publish.json            # Curation output: articles selected for current session
```

---

## API Budget & Ingestion Strategy

To operate entirely on the **Newsdata.io Free Tier** (200 credits/day), the bot uses a strict sampling and scheduling strategy:
* **7 Sessions per Day:** Orchestrated by Prefect, running approximately every 3.5 hours.
* **30 Requests per Session:** In each session, the bot randomly samples 5 domains from the whitelist and runs 6 broad topic queries against them (5 × 6 = 30 credits).
* **State Management:** The exact state of the last fetch is saved locally in `data/last_news.json` so the pipeline can gracefully resume without fetching duplicate pages.
* **Database Deduplication:** Handled safely via PostgreSQL `UNIQUE` constraints and `ON CONFLICT` skips.

## Daily News Flow — 6 Tasks

Each Prefect run executes the following tasks sequentially:

| # | Task | What it does |
|---|------|--------------|
| 1 | `task1-load-config` | Loads and validates `settings.yml`, `sources_whitelist.yml`, and all env vars |
| 2 | `task2-run-ingestion` | Fetches articles from newsdata.io; rotates domain group; saves `last_news.json` |
| 3 | `task3-insert-to-db` | Inserts new articles into `newsbot` DB; skips duplicates via `ON CONFLICT` |
| 4 | `task4-categorize-backlog` | Sends uncategorized titles to OpenRouter in batches; writes `category` column |
| 5 | `task5-curate-articles` | Asks OpenRouter to select the best articles for this session; writes `to_publish.json` |
| 6 | `task6-publish-to-telegram` | Posts each selected article to the Telegram channel; marks `publicated=TRUE` in DB |

**Duplicate publication protection:** Task 6 is gated on Task 5's in-session result — a leftover `to_publish.json` from a previous run is never used. After each successful post, the article is immediately marked `publicated=TRUE` so Task 5 never selects it again.

## LLM Models (OpenRouter)

The bot uses two models with different cost/quality tradeoffs:

- **Categorization (Task 4, high volume):** A fast, free/cheap model (e.g., `nvidia/nemotron-nano-9b-v2`) processes large batches of raw titles and assigns taxonomy labels (e.g., `"AI Policy"`, `"Humanoid Robots"`).
- **Curation (Task 5, low volume):** A higher-quality model (e.g., `nvidia/nemotron-3-ultra-550b`) evaluates the categorized backlog and selects the best articles for publication, using recently published articles as diversity context.

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
```

**Key config knobs** (all in `config/settings.yml`):

| Setting | Controls |
|---|---|
| `session.credits` | Max newsdata.io requests per run |
| `session.domains_per_session` | Domains sampled per run |
| `llm.timeout` | OpenRouter HTTP timeout (seconds), shared by tasks 4 & 5 |
| `llm.categorization.batch_size` | Titles sent to LLM per round in task 4 |
| `llm.categorization.tokens_per_title` | `max_tokens = max(min_tokens, batch × tokens_per_title)` |
| `llm.categorization.min_tokens` | Floor for computed `max_tokens` |
| `llm.categorization.max_total_rounds` | Hard loop cap for task 4 |
| `llm.categorization.poison_mode` | `"mark"` silently labels bad titles; `"fail"` halts the flow |
| `llm.curation.batch_size` | Max candidate articles fetched for task 5 |
| `llm.curation.rag_context_size` | Recently published articles used as diversity context |
| `llm.curation.max_selected` | Max articles posted per session |
| `llm.curation.max_tokens` | Hard cap on curation LLM response length |

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

# Register/update Prefect deployment
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
