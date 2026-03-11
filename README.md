# AI & Robotics News Curation Bot

A **self-hosted, fully automated Telegram bot** that curates and delivers the most interesting, unusual, and high-quality news on Artificial Intelligence (AI) and Robotics directly to the [AI and Robotics News](https://t.me/robotics_ai_news) Telegram Channel.

## Prefect Server Self-hosted basic setup

This project assumes you run a local self-hosted Prefect Server (API + UI) on your PC and connect one or more worker stacks to it over a shared Docker network.

The worker stack (this repo) connects to the server via Docker DNS on a shared external network.

### 1. One-time network setup

Create the shared external Docker network once:

```sh
docker network create prefect-net
```

Both the Prefect Server stack and all worker stacks must attach to this same external network.

### 2. Start the Prefect Server stack

For local-only purpose you can use this basic setup based on [tutorial](https://docs.prefect.io/v3/how-to-guides/self-hosted/docker-compose#how-to-run-the-prefect-server-via-docker-compose)

```yml
services:
  postgres:
    image: postgres:14
    environment:
      POSTGRES_USER: prefect
      POSTGRES_PASSWORD: prefect
      POSTGRES_DB: prefect
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U prefect"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - prefect-net

  redis:
    image: redis:7
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD-SHELL", "redis-cli ping"]
      interval: 5s
      timeout: 5s
      retries: 5
    networks:
      - prefect-net

  prefect-server:
    image: prefecthq/prefect:3-latest
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    environment:
      PREFECT_API_DATABASE_CONNECTION_URL: postgresql+asyncpg://prefect:prefect@postgres:5432/prefect
      PREFECT_SERVER_API_HOST: 0.0.0.0
      PREFECT_SERVER_UI_API_URL: http://localhost:4200/api
      PREFECT_MESSAGING_BROKER: prefect_redis.messaging
      PREFECT_MESSAGING_CACHE: prefect_redis.messaging
      PREFECT_REDIS_MESSAGING_HOST: redis
      PREFECT_REDIS_MESSAGING_PORT: 6379
      PREFECT_REDIS_MESSAGING_DB: 0
    command: prefect server start --no-services
    ports:
      - "4200:4200"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request as u; u.urlopen('http://localhost:4200/api/health', timeout=1)"
        ]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 60s
    networks:
      - prefect-net

  prefect-services:
    image: prefecthq/prefect:3-latest
    depends_on:
      prefect-server:
        condition: service_healthy
    environment:
      PREFECT_API_DATABASE_CONNECTION_URL: postgresql+asyncpg://prefect:prefect@postgres:5432/prefect
      PREFECT_MESSAGING_BROKER: prefect_redis.messaging
      PREFECT_MESSAGING_CACHE: prefect_redis.messaging
      PREFECT_REDIS_MESSAGING_HOST: redis
      PREFECT_REDIS_MESSAGING_PORT: 6379
      PREFECT_REDIS_MESSAGING_DB: 0
    command: prefect server services start
    networks:
      - prefect-net

volumes:
  postgres_data:
  redis_data:

networks:
  prefect-net:
    external: true
```

Save this file as `run_prefect_server.yml` and from its directory run the prefect server:

```sh
docker compose -f run_prefect_server.yml up -d
```

Check if server is running. Open Prefect UI:

- <http://localhost:4200>

Health endpoint:

- <http://localhost:4200/api/health>

### 3. How workers connect

When a worker container is attached to `prefect-net`, it should use the server’s Docker service name:

```yml
PREFECT_API_URL=http://prefect-server:4200/api
```

(Do not use `localhost` from inside a container; inside the container `localhost` refers to the container itself.)

To prove DNS + connectivity from inside the network, run a temporary container on `prefect-net`:

```sh
docker run --rm --network prefect-net curlimages/curl \
  curl -sS http://prefect-server:4200/api/health && echo ""
```

It should return `true`.

### 4. Persistence and data location (named volumes)

The server compose uses Docker named volumes for persistence (Postgres/Redis). On Linux, Docker typically stores named volumes under Docker’s root directory (often `/var/lib/docker`).

Useful commands:

```sh
docker volume ls
docker volume inspect <volume_name>   (look for “Mountpoint”)
docker info | grep -i "Docker Root Dir"
```

If you reinstall/wipe the OS, named volumes will be lost unless you back up the database (recommended: logical backup via `pg_dump`).

## Environment setup

Make project dir and clone repo into it:

```sh
mkdir news_curation_agent
cd news_curation_agent
git clone https://github.com/lexmaister/ai_robotics_news_bot.git
# add private dir to store secrets and settings
mkdir -p private/env
```

## OpenRouter test flow (dev)

This project includes a simple Prefect flow to validate **OpenRouter** connectivity (LLM call) from a dockerized client container that can also reach the local Prefect server over the shared Docker network.

### Prerequisites

- Prefect server is running
- `prefect-net` is up

### Setup Environment

Create `private/env/prefect.dev.env` file and add to it:

```sh
PREFECT_API_URL=http://prefect-server:4200/api
OPENROUTER_API_KEY=...

# Optional
OPENROUTER_MODEL=stepfun/step-3.5-flash:free
PREFECT_LOGGING_LEVEL=INFO
```

### Dev runner container (does not auto-run flows)

Dev compose in that case:

- uses `prefecthq/prefect:3-latest`
- joins the external network `prefect-net`
- installs Python deps from `requirements.txt`
- stays running (so you can run flows manually via `docker exec`)

Start the dev container (from repo root `news_curation_agent/ai_robotics_news_bot/`):

```sh
docker compose -f compose/test_openrouter.yml up -d
```

Check while container is become `healthy`:

```sh
docker inspect -f '{{.State.Health.Status}}' ai-news-worker-dev
```

Run the OpenRouter test flow manually:

```sh
docker exec -it ai-news-worker-dev python -m src.flows.test_openrouter
```

Check flow result in [Prefect server web](http://localhost:4200/dashboard)

View logs (optional):

```sh
docker logs -f ai-news-worker-dev
```

Stop:

```sh
docker compose -f compose/docker-compose.dev.yml down
```

### Health checks / troubleshooting

- Prefect server health (from host):
  - <http://localhost:4200/api/health>

- Container-to-container Prefect API target (from containers on `prefect-net`):
  - PREFECT_API_URL=<http://prefect-server:4200/api>

If the OpenRouter flow fails, first confirm `OPENROUTER_API_KEY` is present in `private/env/prefect.dev.env` and that `requirements.txt` includes the required `openai` package.
