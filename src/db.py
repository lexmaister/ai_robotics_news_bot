"""
src/db.py

Synchronous (psycopg2) DB helpers for the newsbot schema.
Covers: article insert/dedup, batch title fetch, category update,
publication marking, and curation candidate queries.
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


@dataclass(frozen=True)
class EmbeddingRow:
    """Published article with its embedding vector, used for weekly report clustering."""

    id: int
    title: str
    category: Optional[str]
    embedding: list[float]
    request_domain: Optional[str]


def connect(cfg: DbConfig) -> PgConnection:
    """Open a synchronous psycopg2 connection."""
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
    """Parse a NewsData pubDate string ('2026-06-17 09:15:08') to a UTC datetime."""
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
    Insert a single article row; skip on UNIQUE conflict (title or link).

    Returns (inserted, article_id). When inserted=False the article was already
    in the DB; article_id is still resolved via a SELECT fallback.
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


def fetch_titles_for_categorization(
    conn: PgConnection, *, limit: int
) -> list[ArticleTitle]:
    """Fetch up to `limit` uncategorized article titles (category IS NULL), newest first."""
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
    """Write the LLM-assigned category for a single article."""
    with conn.cursor() as cur:
        cur.execute(UPDATE_CATEGORY_SQL, {"id": article_id, "category": category})


def mark_publicated(conn: PgConnection, *, article_id: int) -> None:
    """Mark an article as published (publicated = TRUE)."""
    with conn.cursor() as cur:
        cur.execute(MARK_PUBLICATED_SQL, {"id": article_id})


# ---------- curation queries ----------


@dataclass(frozen=True)
class ArticleCandidate:
    """Categorized, unpublicated article ready for curation."""

    id: int
    title: str
    category: str
    link: str


@dataclass(frozen=True)
class PublishedContext:
    """Recently published article used as temporal RAG diversity context."""

    title: str
    category: str


SELECT_CANDIDATES_FOR_CURATION_SQL = """
SELECT id, title, category, link
FROM articles
WHERE category IS NOT NULL
  AND publicated = FALSE
ORDER BY collected_dt DESC, id DESC
LIMIT %(limit)s;
"""

SELECT_RECENT_PUBLISHED_CONTEXT_SQL = """
SELECT title, category
FROM articles
WHERE publicated = TRUE
  AND category IS NOT NULL
ORDER BY pub_date DESC NULLS LAST, id DESC
LIMIT %(limit)s;
"""


def fetch_candidates_for_curation(
    conn: PgConnection, *, limit: int
) -> list[ArticleCandidate]:
    """
    Fetch categorized, unpublicated articles for curation (no embedding column needed).

    Returns articles in reverse-chronological order (most recently collected first).
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")

    with conn.cursor() as cur:
        cur.execute(SELECT_CANDIDATES_FOR_CURATION_SQL, {"limit": limit})
        rows = cur.fetchall() or []

    return [
        ArticleCandidate(
            id=int(r[0]), title=str(r[1]), category=str(r[2]), link=str(r[3])
        )
        for r in rows
    ]


def fetch_recent_published_context(
    conn: PgConnection, *, limit: int
) -> list[PublishedContext]:
    """
    Fetch recently published articles for curation diversity context (temporal RAG).

    Results are ordered by pub_date DESC so the most recently published appear first.
    Only articles with a non-null category are included.
    """
    if limit <= 0:
        raise ValueError("limit must be > 0")

    with conn.cursor() as cur:
        cur.execute(SELECT_RECENT_PUBLISHED_CONTEXT_SQL, {"limit": limit})
        rows = cur.fetchall() or []

    return [PublishedContext(title=str(r[0]), category=str(r[1])) for r in rows]


# ---------- embedding queries ----------

SELECT_PUBLISHED_WITHOUT_EMBEDDING_SQL = """
SELECT id, title
FROM articles
WHERE publicated = TRUE
  AND embedding IS NULL
ORDER BY id DESC
LIMIT %(limit)s;
"""

UPDATE_EMBEDDING_SQL = """
UPDATE articles
SET embedding = %(embedding)s::vector
WHERE id = %(id)s;
"""

# ---------- report / cleanup queries ----------

DELETE_OLD_UNPUBLISHED_SQL = """
DELETE FROM articles
WHERE publicated = FALSE
  AND collected_dt < NOW() - INTERVAL '1 day' * %(days)s;
"""

SELECT_PUBLISHED_EMBEDDINGS_SQL = """
SELECT id, title, category, embedding::text, request_domain
FROM articles
WHERE publicated = TRUE
  AND embedding IS NOT NULL
  AND collected_dt >= NOW() - INTERVAL '1 day' * %(days)s
ORDER BY collected_dt DESC
LIMIT %(limit)s;
"""


def fetch_published_without_embedding(
    conn: PgConnection, *, limit: int
) -> list[ArticleTitle]:
    """Fetch up to `limit` published articles that have no embedding yet, newest first."""
    if limit <= 0:
        raise ValueError("limit must be > 0")

    with conn.cursor() as cur:
        cur.execute(SELECT_PUBLISHED_WITHOUT_EMBEDDING_SQL, {"limit": limit})
        rows = cur.fetchall() or []

    return [ArticleTitle(id=int(r[0]), title=str(r[1])) for r in rows]


def update_embedding(
    conn: PgConnection, *, article_id: int, embedding: list[float]
) -> None:
    """Write the embedding vector for a single published article."""
    vec_str = "[" + ",".join(str(v) for v in embedding) + "]"
    with conn.cursor() as cur:
        cur.execute(UPDATE_EMBEDDING_SQL, {"id": article_id, "embedding": vec_str})


# ---------- report / cleanup operations ----------


def _parse_vector(vec_text: str) -> list[float]:
    """Parse a PostgreSQL vector text '[0.1,0.2,...]' into a list of floats."""
    return [float(x) for x in vec_text.strip("[]").split(",")]


def cleanup_old_unpublished(conn: PgConnection, *, older_than_days: int) -> int:
    """
    Delete unpublished articles whose collected_dt is older than `older_than_days` days.

    Intended to reclaim DB storage between weekly report runs.
    The caller is responsible for committing the transaction.

    Returns:
        Number of rows deleted.
    """
    if older_than_days <= 0:
        raise ValueError("older_than_days must be > 0")

    with conn.cursor() as cur:
        cur.execute(DELETE_OLD_UNPUBLISHED_SQL, {"days": older_than_days})
        return cur.rowcount


def fetch_published_embeddings_for_report(
    conn: PgConnection, *, lookback_days: int, limit: int
) -> list[EmbeddingRow]:
    """
    Fetch published articles with non-null embedding vectors collected within the
    last `lookback_days` days. Used by the weekly report clustering pipeline.

    Args:
        lookback_days: How many days back to look (driven by orchestration.report_interval_days).
        limit:         Maximum number of rows to return (driven by report.max_articles_to_analyze).

    Returns:
        List of EmbeddingRow sorted by collected_dt DESC (most recent first).
    """
    if lookback_days <= 0:
        raise ValueError("lookback_days must be > 0")
    if limit <= 0:
        raise ValueError("limit must be > 0")

    with conn.cursor() as cur:
        cur.execute(
            SELECT_PUBLISHED_EMBEDDINGS_SQL, {"days": lookback_days, "limit": limit}
        )
        rows = cur.fetchall() or []

    result: list[EmbeddingRow] = []
    for row in rows:
        article_id = int(row[0])
        title = str(row[1])
        category = str(row[2]) if row[2] is not None else None
        embedding = _parse_vector(str(row[3]))
        request_domain = str(row[4]) if row[4] is not None else None
        result.append(
            EmbeddingRow(
                id=article_id,
                title=title,
                category=category,
                embedding=embedding,
                request_domain=request_domain,
            )
        )
    return result
