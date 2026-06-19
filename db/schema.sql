-- db/schema.sql
-- Manual schema for the `newsbot` database (PostgreSQL + pgvector).
-- USAGE: docker exec -i ai_robotics_news_bot-postgres-1 psql -U prefect -d newsbot < db/schema.sql

BEGIN;

-- Ensure extension exists (safe even if already created in init).
CREATE EXTENSION IF NOT EXISTS vector;

-- Main table
CREATE TABLE IF NOT EXISTS articles (
    id BIGSERIAL PRIMARY KEY,

    -- Dedup keys (aggressive by design)
    title TEXT NOT NULL UNIQUE,
    link  TEXT NOT NULL UNIQUE,

    -- Source metadata we keep (from last_news.json)
    pub_date TIMESTAMPTZ NULL,
    request_domain TEXT NULL,
    request_query_name TEXT NULL,
    request_query_field TEXT NULL,
    collected_dt TIMESTAMPTZ NOT NULL,

    -- LLM metadata
    category TEXT NULL,

    -- Publishing state
    publicated BOOLEAN NOT NULL DEFAULT FALSE,

    -- Embedding is only expected to be filled when publicated = true
    embedding VECTOR(1536) NULL
);

-- Helpful indexes for typical queries
CREATE INDEX IF NOT EXISTS idx_articles_publicated
    ON articles(publicated);

CREATE INDEX IF NOT EXISTS idx_articles_collected_dt
    ON articles(collected_dt DESC);

CREATE INDEX IF NOT EXISTS idx_articles_pub_date
    ON articles(pub_date DESC);

CREATE INDEX IF NOT EXISTS idx_articles_category
    ON articles(category);

-- Optional: prevent embeddings on non-publicated rows at DB level.
-- For strict enforcement, uncomment this check.
-- (Note: existing rows must satisfy it.)
-- ALTER TABLE articles
--   ADD CONSTRAINT chk_embedding_only_when_publicated
--   CHECK (publicated OR embedding IS NULL);

COMMIT;
