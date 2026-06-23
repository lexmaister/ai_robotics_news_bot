"""
src/db.py

DB connection and minimal queries for the simplified newsbot schema.

Table: articles
- id (PK)
- title (UNIQUE)
- link (UNIQUE)
- pub_date
- request_domain
- request_query_name
- request_query_field
- collected_dt
- category (LLM)
- publicated (bool)
- embedding (pgvector) -- only set when publicated = True

New in Phase 2/3:
- Fetch batches of uncategorized titles for LLM categorization.

NOTE: This module is intentionally synchronous (psycopg2) and simple.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PgConnection


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    dbname: str = "newsbot"


@dataclass(frozen=True)
class ArticleTitle:
    """Minimal payload for title categorization."""

    id: int
    title: str


def connect(cfg: DbConfig) -> PgConnection:
    """
    Open a synchronous connection to Postgres (psycopg2).

    Further development:
    - Consider connection pooling if throughput increases.
    - Consider explicit transactions for batch operations.
    """
    conn = psycopg2.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        dbname=cfg.dbname,
    )
    return conn


# ---------- helpers ----------

def _parse_pub_date(pub_date_raw: Optional[str]) -> Optional[datetime]:
    """
    Parse NewsData 'pubDate' string.

    In last_news.json it looks like: '2026-06-17 09:15:08'

    Further development:
    - If provider changes format, add more parsing strategies.
    - Decide if you want TIMESTAMPTZ always in UTC (recommended).
    """
    if not pub_date_raw:
        return None
    try:
        dt = datetime.strptime(pub_date_raw, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- core operations ----------

INSERT_ARTICLE_SQL = """
INSERT INTO articles (
    title,
    link,
    pub_date,
    request_domain,
    request_query_name,
    request_query_field,
    collected_dt,
    category,
    publicated,
    embedding
)
VALUES (
    %(title)s,
    %(link)s,
    %(pub_date)s,
    %(request_domain)s,
    %(request_query_name)s,
    %(request_query_field)s,
    %(collected_dt)s,
    NULL,
    FALSE,
    NULL
)
ON CONFLICT DO NOTHING
RETURNING id;
"""

SELECT_ID_BY_LINK_SQL = """
SELECT id FROM articles WHERE link = %(link)s LIMIT 1;
"""

UPDATE_CATEGORY_SQL = """
UPDATE articles
SET category = %(category)s
WHERE id = %(id)s;
"""

MARK_PUBLICATED_SQL = """
UPDATE articles
SET publicated = TRUE
WHERE id = %(id)s;
"""

UPDATE_EMBEDDING_SQL = """
UPDATE articles
SET embedding = %(embedding)s
WHERE id = %(id)s
  AND publicated = TRUE;
"""

# Titles needing categorization (category is NULL).
# We do not filter on publicated here; typically publicated rows will already have a category.
SELECT_TITLES_FOR_CATEGORIZATION_SQL = """
SELECT id, title
FROM articles
WHERE category IS NULL
ORDER BY collected_dt DESC, id DESC
LIMIT %(limit)s;
"""


def insert_or_skip_article(
    conn: PgConnection,
    *,
    title: str,
    link: str,
    pub_date_raw: Optional[str],
    request_domain: Optional[str],
    request_query_name: Optional[str],
    request_query_field: Optional[str],
    collected_dt_raw: str,
) -> Tuple[bool, Optional[int]]:
    """
    Insert a single article row, skipping if it conflicts with existing UNIQUE(title/link).

    Returns:
      (inserted, article_id)

    inserted=False means either:
      - duplicate in current run (same title/link), OR
      - already present in DB from previous runs.

    Further development:
    - If you later want to track multiple observations, move request_* into a separate table.
    - If you want "first wins" consistently, keep DO NOTHING.
      If you want "last wins", change ON CONFLICT DO UPDATE (but you'll overwrite request_*).
    """
    pub_date = _parse_pub_date(pub_date_raw)
    collected_dt = datetime.fromisoformat(collected_dt_raw)

    params: dict[str, Any] = {
        "title": title,
        "link": link,
        "pub_date": pub_date,
        "request_domain": request_domain,
        "request_query_name": request_query_name,
        "request_query_field": request_query_field,
        "collected_dt": collected_dt,
    }

    with conn.cursor() as cur:
        cur.execute(INSERT_ARTICLE_SQL, params)
        row = cur.fetchone()
        if row:
            return True, int(row[0])

        # If skipped, we usually don't need the id. But returning it can help later steps.
        cur.execute(SELECT_ID_BY_LINK_SQL, {"link": link})
        row2 = cur.fetchone()
        return False, (int(row2[0]) if row2 else None)


def fetch_titles_for_categorization(conn: PgConnection, *, limit: int) -> list[ArticleTitle]:
    """
    Fetch a batch of uncategorized article titles.

    The caller should pass:
      limit = settings.llm.categorization.batch_size

    Returns a list of (id, title) in the order they should be sent to the model.

    Further development:
    - Add a "locked_by/locked_dt" mechanism if you run multiple workers concurrently.
    - Add optional time window filters (e.g., last N days).
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")

    with conn.cursor() as cur:
        cur.execute(SELECT_TITLES_FOR_CATEGORIZATION_SQL, {"limit": limit})
        rows = cur.fetchall() or []

    out: list[ArticleTitle] = []
    for row in rows:
        article_id, title = int(row[0]), str(row[1])
        out.append(ArticleTitle(id=article_id, title=title))
    return out


def update_category(conn: PgConnection, *, article_id: int, category: str) -> None:
    """
    Persist LLM category to DB.

    Further development:
    - Enforce a taxonomy list (validate category before update).
    - Store model name / prompt version / confidence if you later need audits.
    """
    with conn.cursor() as cur:
        cur.execute(UPDATE_CATEGORY_SQL, {"id": article_id, "category": category})


def mark_publicated(conn: PgConnection, *, article_id: int) -> None:
    """
    Mark an article as published to the Telegram channel.

    Further development:
    - Save telegram_message_id / posted_dt if you need re-post protection and audit trail.
    """
    with conn.cursor() as cur:
        cur.execute(MARK_PUBLICATED_SQL, {"id": article_id})


def update_embedding(conn: PgConnection, *, article_id: int, embedding: list[float]) -> None:
    """
    Store embedding vector, but only if publicated = TRUE.

    NOTE: With psycopg2 + pgvector, you typically need a type adapter to pass Python lists
    directly into a VECTOR column.

    Further development:
    - Register pgvector adapter for psycopg2, or serialize appropriately.
    - Store embedding_model and embedded_dt for traceability.
    - If you later want embeddings for *all* articles, drop the publicated gate.
    """
    with conn.cursor() as cur:
        cur.execute(UPDATE_EMBEDDING_SQL, {"id": article_id, "embedding": embedding})
