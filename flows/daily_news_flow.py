"""
flows/daily_news_flow.py

Production Prefect flow for daily AI/Robotics news ingestion.

Stages:
1) Load EnvSettings (paths + API key) injected via docker-compose / Prefect worker env.
2) Load/validate settings.yml (includes NewsData query-length guard <= 100).
3) Load whitelist groups/domains from sources_whitelist.yml.
4) Run ingestion via src.ingestion.run_ingestion(...).
5) Insert articles into Postgres newsbot DB (skip duplicates via UNIQUE constraints).
6) Return a JSON-serializable payload (for later downstream tasks).
"""

from __future__ import annotations

import os

from prefect import flow, get_run_logger, task

from src.config import EnvSettings, load_settings, load_whitelist
from src.ingestion import run_ingestion
from src.db import DbConfig, connect, insert_or_skip_article


@task(name="task3-insert-to-db")
def insert_to_db_task(articles: list[dict], collected_dt: str) -> dict:
    """
    Task 3: Insert ingested articles into newsbot DB.

    Behavior:
    - Iterate articles one-by-one.
    - Try INSERT ... ON CONFLICT DO NOTHING (duplicates in JSON or DB are skipped).
    - Return counts + inserted ids (useful for later categorization step).

    Further development (logic description only):
    - Add Task 4: categorize only inserted_ids and update category.
    - Add publishing step: set publicated=true after posting to Telegram.
    - Add embedding step: compute/store embedding only for publicated=true rows.
    """
    log = get_run_logger()

    cfg = DbConfig(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="newsbot",
    )

    inserted_ids: list[int] = []
    skipped = 0

    conn = connect(cfg)
    try:
        for a in articles:
            inserted, article_id = insert_or_skip_article(
                conn,
                title=a.get("title"),
                link=a.get("link"),
                pub_date_raw=a.get("pubDate"),
                request_domain=a.get("request_domain"),
                request_query_name=a.get("request_query_name"),
                request_query_field=a.get("request_query_field"),
                collected_dt_raw=collected_dt,
            )

            if inserted and article_id is not None:
                inserted_ids.append(article_id)
            else:
                skipped += 1

        conn.commit()
    finally:
        conn.close()

    log.info("DB insert complete: inserted=%s skipped=%s total=%s", len(inserted_ids), skipped, len(articles))
    return {"inserted": len(inserted_ids), "skipped": skipped, "inserted_ids": inserted_ids}


@flow(name="daily-news-flow")
def daily_news_flow() -> dict:
    """
    Main production flow entrypoint.

    Returns:
        dict: JSON-serializable run payload (safe to store as artifact / pass to tasks).
    """
    log = get_run_logger()

    # ----------------------------
    # Task 1) Load env + config
    # ----------------------------
    env = EnvSettings()

    settings = load_settings(env)      # validates YAML + query length constraint
    whitelist = load_whitelist(env)    # groups -> list[domains]

    group_names = sorted(whitelist.keys())
    domains_total = sum(len(whitelist[g]) for g in group_names)

    log.info("Config loaded successfully.")
    log.info(
        "Paths: settings=%s whitelist=%s prompts_dir=%s last_news=%s",
        env.settings_path,
        env.sources_whitelist_path,
        env.prompts_dir,
        env.last_news_path,
    )
    log.info(
        "Settings: credits=%s domains_per_session=%s language=%s size=%s timeframe=%s query_field_mode=%s queries=%s",
        settings.session.credits,
        settings.session.domains_per_session,
        settings.newsdata.language,
        settings.newsdata.size,
        settings.newsdata.timeframe,
        settings.newsdata.query_field_mode,
        len(settings.queries),
    )
    log.info(
        "Whitelist: groups=%s domains_total=%s group_sizes=%s",
        len(group_names),
        domains_total,
        {g: len(whitelist[g]) for g in group_names},
    )

    # ----------------------------
    # Task 2) Run ingestion
    # ----------------------------
    result = run_ingestion(
        settings=settings,
        whitelist_groups=whitelist,
        last_news_path=env.last_news_path,
        newsdata_api_key=env.newsdata_api_key.get_secret_value(),
    )

    log.info(
        "Ingestion result: status=%s group=%s requests=%s sampled_domains=%s articles=%s collected_dt=%s",
        result.status,
        result.active_group,
        result.requests_planned,
        result.sampled_domains,
        result.articles_collected,
        result.collected_dt,
    )

    # Policy: treat "no_plan" as a non-fatal condition (Completed run with warning).
    # If you prefer failing the run for alerting, replace with: raise RuntimeError(...)
    if result.status == "no_plan":
        log.warning(
            "No plan created (no API calls). Check whitelist group domains: group=%s",
            result.active_group,
        )

    # ----------------------------
    # Task 3) Insert to DB
    # ----------------------------
    storage = insert_to_db_task(result.articles, result.collected_dt)

    return {
        "config": {
            "paths": {
                "settings_path": str(env.settings_path),
                "sources_whitelist_path": str(env.sources_whitelist_path),
                "prompts_dir": str(env.prompts_dir),
                "last_news_path": str(env.last_news_path),
            },
            "settings": {
                "credits": settings.session.credits,
                "domains_per_session": settings.session.domains_per_session,
                "newsdata": {
                    "language": settings.newsdata.language,
                    "timeframe": settings.newsdata.timeframe,
                    "size": settings.newsdata.size,
                    "removeduplicate": settings.newsdata.removeduplicate,
                    "query_field_mode": settings.newsdata.query_field_mode,
                    "excludefield": settings.newsdata.excludefield,
                },
                "queries_count": len(settings.queries),
                "query_names": [q.name for q in settings.queries],
            },
            "whitelist": {
                "groups": group_names,
                "domains_total": domains_total,
                "group_sizes": {g: len(whitelist[g]) for g in group_names},
            },
        },
        "ingestion": {
            "status": result.status,
            "active_group": result.active_group,
            "requests_planned": result.requests_planned,
            "sampled_domains": result.sampled_domains,
            "articles_collected": result.articles_collected,
            "collected_dt": result.collected_dt,
            "plan": result.plan,
            "articles": result.articles,
        },
        "storage": storage,
    }


if __name__ == "__main__":
    daily_news_flow()
