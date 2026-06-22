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
├── .env.example                  # Template with placeholders (tracked)
├── .gitignore
│
├── config/                       # Declarative configuration (YAML)
│   ├── sources_whitelist.yml     # Allowed newsdata.io sources + priority scores
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
│   ├── embeddings.py             # Embedding model calls
│   ├── ingestion.py              # newsdata.io fetcher
│   ├── curation.py               # LLM logic: categorization and curation(OpenRouter)
│   ├── publisher.py              # Telegram posting
│   └── analytics.py              # Metrics and reporting
│
├── flows/                        # Prefect flow definitions
│   ├── daily_news_flow.py
│   └── analytics_flow.py
│
└── tests/
```

---

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/lexmaister/ai_robotics_news_bot.git
cd ai_robotics_news_bot
cp .env.example .env
# Edit .env with your actual secrets
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
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
CURATION_MODEL=mistralai/mistral-large-latest

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
docker exec -it ai_robotics_news_bot-worker-1 python -m flows.daily_news_flow

# Check Prefect API health
curl -s http://localhost:4200/api/health

# View logs
docker compose logs -f worker
docker compose logs -f prefect-server
```

---

## Persistence and Backups

PostgreSQL data is stored in a Docker named volume (`postgres_data`). If the host OS is reinstalled, volumes are lost.

Recommended periodic backups:

```bash
docker exec ai_robotics_news_bot-postgres-1 pg_dump -U admin newsbot > backup_newsbot.sql
docker exec ai_robotics_news_bot-postgres-1 pg_dump -U admin prefect > backup_prefect.sql
```

---

## License

MIT
