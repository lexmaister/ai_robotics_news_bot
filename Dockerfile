FROM python:3.13-slim

WORKDIR /app

# System dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install all Python dependencies directly
RUN pip install --no-cache-dir \
    prefect==3.* \
    openai \
    httpx \
    python-telegram-bot \
    psycopg2-binary \
    pgvector \
    sqlalchemy \
    numpy \
    tiktoken \
    python-dotenv \
    pydantic \
    pydantic-settings

# Copy project source code
COPY src/ ./src/
COPY flows/ ./flows/

# Default command: start Prefect worker connecting to the work pool
CMD ["prefect", "worker", "start", "--pool", "news-bot-pool"]
