"""
flows/daily_news_flow.py

Production Prefect flow for daily AI/Robotics news ingestion.

Stages (as Prefect tasks):
1) Task 1: Load EnvSettings + load/validate YAML config + load whitelist.
2) Task 2: Run ingestion via src.ingestion.run_ingestion(...).
3) Task 3: Insert articles into Postgres newsbot DB (skip duplicates via UNIQUE constraints).
4) Task 4: Categorize uncategorized titles backlog via OpenRouter (strict JSON) and update articles.category.

Design notes:
- No concurrency: we categorize from backlog with no locking.
- Fail-fast: if the model output is invalid, Task 4 raises and the flow fails.
"""

from __future__ import annotations

import os
from typing import Any

from prefect import flow, get_run_logger, task

from src.config import EnvSettings, load_settings, load_whitelist
from src.ingestion import run_ingestion
from src.db import DbConfig, connect, fetch_titles_for_categorization, insert_or_skip_article, update_category
from src.curation import categorize_titles_via_openrouter


def _db_config_from_env() -> DbConfig:
    return DbConfig(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="newsbot",
    )


@task(name="task1-load-config")
def load_config_task() -> dict[str, Any]:
    """Task 1: load environment settings, settings.yml, and sources whitelist."""
    log = get_run_logger()

    env = EnvSettings()
    settings = load_settings(env)
    whitelist = load_whitelist(env)

    group_names = sorted(whitelist.keys())
    domains_total = sum(len(whitelist[g]) for g in group_names)

    log.info("Config loaded successfully.")
    log.info(
        "Paths: settings=%s whitelist=%s categorization_prompt=%s curation_prompt=%s last_news=%s",
        env.settings_path,
        env.sources_whitelist_path,
        env.categorization_prompt_path,
        env.curation_prompt_path,
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
        "LLM: categorization_model=%s curation_model=%s categorization_batch_size=%s",
        settings.llm.categorization_model,
        settings.llm.curation_model,
        settings.llm.categorization_batch_size,
    )

    log.info(
        "Whitelist: groups=%s domains_total=%s group_sizes=%s",
        len(group_names),
        domains_total,
        {g: len(whitelist[g]) for g in group_names},
    )

    return {
        "env": {
            "settings_path": str(env.settings_path),
            "sources_whitelist_path": str(env.sources_whitelist_path),
            "categorization_prompt_path": str(env.categorization_prompt_path),
            "curation_prompt_path": str(env.curation_prompt_path),
            "last_news_path": str(env.last_news_path),
        },
        "settings_obj": settings,
        "whitelist_obj": whitelist,
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
    """Task 2: run ingestion."""
    log = get_run_logger()

    settings = cfg["settings_obj"]
    whitelist = cfg["whitelist_obj"]

    # Reconstruct EnvSettings to read secrets.
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
    """Task 3: Insert ingested articles into newsbot DB."""
    log = get_run_logger()

    cfg = _db_config_from_env()
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


@task(name="task4-categorize-backlog")
def categorize_backlog_task(cfg: dict[str, Any]) -> dict[str, Any]:
    """Task 4: Categorize uncategorized titles from backlog and update articles.category."""
    log = get_run_logger()

    settings = cfg["settings_obj"]
    env = EnvSettings()  # for secrets + prompt paths

    batch_size = int(settings.llm.categorization_batch_size)
    model = str(settings.llm.categorization_model)

    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)

    updated = 0
    rounds = 0
    max_rounds = 10  # safety cap per flow run

    try:
        while rounds < max_rounds:
            rows = fetch_titles_for_categorization(conn, limit=batch_size)
            if not rows:
                break

            titles = [r.title for r in rows]
            ids = [r.id for r in rows]

            result = categorize_titles_via_openrouter(
                api_key=env.openrouter_api_key.get_secret_value(),
                model=model,
                template_path=env.categorization_prompt_path,
                titles=titles,
                temperature=0.0,
            )

            # Persist in the same order.
            for article_id, cat in zip(ids, result.categories):
                update_category(conn, article_id=article_id, category=cat)
                updated += 1

            conn.commit()
            rounds += 1
            log.info(
                "Categorization batch complete: batch=%s updated_total=%s model=%s",
                len(ids),
                updated,
                model,
            )

        log.info("Categorization done: updated=%s rounds=%s", updated, rounds)
        return {"updated": updated, "rounds": rounds, "batch_size": batch_size, "model": model}

    finally:
        conn.close()


@flow(name="daily-news-flow")
def daily_news_flow() -> dict:
    """Main production flow entrypoint."""
    cfg = load_config_task()
    ingestion = run_ingestion_task(cfg)
    storage = insert_to_db_task(ingestion["articles"], ingestion["collected_dt"])
    categorization = categorize_backlog_task(cfg)

    return {
        "config": {
            "paths": cfg["env"],
            "settings": {
                "credits": cfg["settings_obj"].session.credits,
                "domains_per_session": cfg["settings_obj"].session.domains_per_session,
                "llm": {
                    "categorization_model": cfg["settings_obj"].llm.categorization_model,
                    "curation_model": cfg["settings_obj"].llm.curation_model,
                    "categorization_batch_size": cfg["settings_obj"].llm.categorization_batch_size,
                },
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
        "categorization": categorization,
    }


if __name__ == "__main__":
    daily_news_flow()

