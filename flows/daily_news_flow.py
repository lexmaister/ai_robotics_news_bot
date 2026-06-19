"""
flows/daily_news_flow.py

Production Prefect flow for daily AI/Robotics news ingestion.

Stages (as Prefect tasks):
1) Task 1: Load EnvSettings + load/validate YAML config + load whitelist.
2) Task 2: Run ingestion via src.ingestion.run_ingestion(...).
3) Task 3: Insert articles into Postgres newsbot DB (skip duplicates via UNIQUE constraints).
4) Return a JSON-serializable payload (for later downstream tasks).
"""

from __future__ import annotations

import os
from typing import Any

from prefect import flow, get_run_logger, task

from src.config import EnvSettings, load_settings, load_whitelist
from src.ingestion import run_ingestion
from src.db import DbConfig, connect, insert_or_skip_article


@task(name="task1-load-config")
def load_config_task() -> dict[str, Any]:
    """
    Task 1: load environment settings, settings.yml, and sources whitelist.

    Returns a JSON-serializable dict that contains:
    - env paths (as strings)
    - settings summary needed downstream
    - whitelist domains grouped
    """
    log = get_run_logger()

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

    # Return a serializable object. We pass back EnvSettings paths and raw objects
    # are not guaranteed to be serializable, so we return summaries + keep settings/whitelist
    # in-memory in the task result via a dict. (Prefect can often handle this, but this is safer.)
    return {
        "env": {
            "settings_path": str(env.settings_path),
            "sources_whitelist_path": str(env.sources_whitelist_path),
            "prompts_dir": str(env.prompts_dir),
            "last_news_path": str(env.last_news_path),
            # We do NOT return secrets.
        },
        "settings_obj": settings,    # used internally by downstream task (Prefect will pass object in-process)
        "whitelist_obj": whitelist,  # used internally by downstream task
        "summary": {
            "group_names": group_names,
            "domains_total": domains_total,
            "group_sizes": {g: len(whitelist[g]) for g in group_names},
            "queries_count": len(settings.queries),
            "query_names": [q.name for q in settings.queries],
        },
    }


@task(name="task2-run-ingestion")
def run_ingestion_task(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    Task 2: run ingestion and return a JSON-serializable dict.
    """
    log = get_run_logger()

    settings = cfg["settings_obj"]
    whitelist = cfg["whitelist_obj"]
    last_news_path = cfg["env"]["last_news_path"]

    # EnvSettings holds the SecretStr; easiest is to reconstruct EnvSettings to read the secret.
    # Further development: pass SecretStr through Prefect Blocks/Variables.
    env = EnvSettings()

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

    if result.status == "no_plan":
        log.warning(
            "No plan created (no API calls). Check whitelist group domains: group=%s",
            result.active_group,
        )

    # Return serializable ingestion output
    return {
        "status": result.status,
        "active_group": result.active_group,
        "requests_planned": result.requests_planned,
        "sampled_domains": result.sampled_domains,
        "articles_collected": result.articles_collected,
        "collected_dt": result.collected_dt,
        "plan": result.plan,
        "articles": result.articles,
    }


@task(name="task3-insert-to-db")
def insert_to_db_task(articles: list[dict], collected_dt: str) -> dict:
    """
    Task 3: Insert ingested articles into newsbot DB.

    Behavior:
    - Iterate articles one-by-one.
    - Try INSERT ... ON CONFLICT DO NOTHING (duplicates in JSON or DB are skipped).
    - Return counts + inserted ids.

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
        dict: JSON-serializable run payload.
    """
    # Task 1
    cfg = load_config_task()

    # Task 2
    ingestion = run_ingestion_task(cfg)

    # Task 3
    storage = insert_to_db_task(ingestion["articles"], ingestion["collected_dt"])

    # Keep your previous response structure, but now fully task-driven
    return {
        "config": {
            "paths": cfg["env"],
            "settings": {
                # keep the same public summary as before
                "credits": cfg["settings_obj"].session.credits,
                "domains_per_session": cfg["settings_obj"].session.domains_per_session,
                "newsdata": {
                    "language": cfg["settings_obj"].newsdata.language,
                    "timeframe": cfg["settings_obj"].newsdata.timeframe,
                    "size": cfg["settings_obj"].newsdata.size,
                    "removeduplicate": cfg["settings_obj"].newsdata.removeduplicate,
                    "query_field_mode": cfg["settings_obj"].newsdata.query_field_mode,
                    "excludefield": cfg["settings_obj"].newsdata.excludefield,
                },
                "queries_count": cfg["summary"]["queries_count"],
                "query_names": cfg["summary"]["query_names"],
            },
            "whitelist": {
                "groups": cfg["summary"]["group_names"],
                "domains_total": cfg["summary"]["domains_total"],
                "group_sizes": cfg["summary"]["group_sizes"],
            },
        },
        "ingestion": ingestion,
        "storage": storage,
    }


if __name__ == "__main__":
    daily_news_flow()
