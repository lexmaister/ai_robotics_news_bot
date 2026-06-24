"""
flows/daily_news_flow.py

Production Prefect flow for daily AI/Robotics news ingestion.

Stages (as Prefect tasks):
1) Task 1: Load EnvSettings + load/validate YAML config + load whitelist.
2) Task 2: Run ingestion via src.ingestion.run_ingestion(...).
3) Task 3: Insert articles into Postgres newsbot DB (skip duplicates via UNIQUE constraints).
4) Task 4: Categorize uncategorized titles backlog via OpenRouter (strict JSON) and update articles.category.
5) Task 5: Curate categorized+unpublicated articles via OpenRouter; write /app/data/to_publish.json.
6) Task 6: Publish curated articles to Telegram channel; mark each as publicated=TRUE in DB immediately after posting.

Design notes:
- No concurrency: tasks run sequentially.
- Fail-fast: OpenAIError or invalid model output raises and fails the flow.
- Graceful skip: empty candidate backlog in Task 5 is a no-op (not a failure).
- Task 6 only runs if Task 5 returned status="ok" with selected > 0 in this session,
  preventing stale to_publish.json from being re-published.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prefect import flow, get_run_logger, task

from src.config import EnvSettings, load_settings, load_whitelist
from src.ingestion import run_ingestion
from src.db import (
    DbConfig,
    connect,
    fetch_candidates_for_curation,
    fetch_recent_published_context,
    fetch_titles_for_categorization,
    insert_or_skip_article,
    mark_publicated,
    update_category,
)
from openai import OpenAIError

from src.curation import (
    categorize_titles_via_openrouter,
    curate_articles_via_openrouter,
)
from src.publishing import (
    ArticleToPublish,
    TelegramPublishError,
    format_article_message,
    send_telegram_message,
)


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
        "LLM: categorization_model=%s curation_model=%s",
        settings.llm.categorization_model,
        settings.llm.curation_model,
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
            title = a.get("title") or ""
            link = a.get("link") or ""
            if not title or not link:
                log.warning(
                    "Skipping article with missing title/link: %s", a.get("article_id")
                )
                skipped += 1
                continue
            inserted, article_id = insert_or_skip_article(
                conn,
                title=title,
                link=link,
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

    log.info(
        "DB insert complete: inserted=%s skipped=%s total=%s",
        len(inserted_ids),
        skipped,
        len(articles),
    )
    return {
        "inserted": len(inserted_ids),
        "skipped": skipped,
        "inserted_ids": inserted_ids,
    }


@task(name="task4-categorize-backlog")
def categorize_backlog_task(cfg: dict[str, Any]) -> dict[str, Any]:
    """Task 4: Categorize uncategorized titles from backlog and update articles.category."""
    log = get_run_logger()

    settings = cfg["settings_obj"]
    env = EnvSettings()  # for secrets + prompt paths

    cat_cfg = settings.llm.categorization
    batch_size = int(cat_cfg.batch_size)
    max_rounds = int(cat_cfg.max_total_rounds)
    min_chunk_size = int(cat_cfg.min_chunk_size)
    poison_mode = str(cat_cfg.poison_mode)  # "fail" | "mark"
    poison_fallback = str(cat_cfg.poison_fallback_category)

    model = str(settings.llm.categorization_model)
    llm_timeout = float(settings.llm.timeout)

    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)

    updated = 0
    rounds = 0

    def categorize_chunk(titles: list[str]) -> list[str]:
        result = categorize_titles_via_openrouter(
            api_key=env.openrouter_api_key.get_secret_value(),
            model=model,
            template_path=env.categorization_prompt_path,
            titles=titles,
            temperature=0.0,
            base_url=settings.llm.openrouter_base_url,
            timeout=llm_timeout,
        )
        return result.categories

    try:
        while rounds < max_rounds:
            rows = fetch_titles_for_categorization(conn, limit=batch_size)
            if not rows:
                log.info("Backlog is empty. No more titles to categorize")
                break

            log.info(
                "Fetched batch of %s uncategorized titles from database", len(rows)
            )

            ids = [r.id for r in rows]
            titles = [r.title for r in rows]

            chunk_size = len(titles)
            i = 0

            while i < len(titles):
                chunk_titles = titles[i : i + chunk_size]
                chunk_ids = ids[i : i + chunk_size]

                try:
                    cats = categorize_chunk(chunk_titles)

                    for article_id, cat in zip(chunk_ids, cats):
                        update_category(conn, article_id=article_id, category=cat)
                        updated += 1

                    conn.commit()
                    log.info(
                        "Successfully categorized %s articles. Total updated so far: %s",
                        chunk_size,
                        updated,
                    )
                    i += chunk_size

                except ValueError as exc:
                    # PROCEED: Model responded, but output was invalid JSON or bad format.
                    conn.rollback()
                    log.warning(
                        "Validation failed: chunk_size=%s err=%s", chunk_size, str(exc)
                    )

                    if chunk_size > min_chunk_size:
                        # Halve the chunk to isolate the hallucination
                        chunk_size = max(min_chunk_size, chunk_size // 2)
                        continue

                    # chunk_size == min_chunk_size: Isolate poison-pill title and proceed
                    if poison_mode == "fail":
                        log.error(
                            "poison_mode='fail': could not categorize article_id=%s. Halting.",
                            chunk_ids[0],
                        )
                        raise RuntimeError(
                            f"Categorization halted (poison_mode='fail') for "
                            f"article_id={chunk_ids[0]}: {exc}"
                        ) from exc

                    log.warning(
                        "Marking poison pill and proceeding: article_id=%s",
                        chunk_ids[0],
                    )
                    update_category(
                        conn, article_id=chunk_ids[0], category=poison_fallback
                    )
                    conn.commit()
                    updated += 1
                    i += 1  # Move past the bad title

                except OpenAIError as exc:
                    # HALT: API is down, rate limited, or network failure.
                    conn.rollback()
                    log.error(
                        "Critical LLM API failure. Halting task. Error: %s", str(exc)
                    )
                    raise  # Stop the flow

                except Exception as exc:
                    # HALT: Unexpected system bug or database disconnect.
                    conn.rollback()
                    log.error(
                        "Unexpected critical error. Halting task. Error: %s", str(exc)
                    )
                    raise  # Stop the flow

                finally:
                    rounds += 1
                    if rounds >= max_rounds:
                        break

        log.info("Categorization done: updated=%s rounds=%s", updated, rounds)
        return {
            "updated": updated,
            "rounds": rounds,
            "batch_size": batch_size,
            "model": model,
        }

    finally:
        conn.close()


@task(name="task5-curate-articles")
def curate_articles_task(cfg: dict[str, Any]) -> dict[str, Any]:
    """Task 5: Select best categorized articles for publication. Writes /app/data/to_publish.json."""
    log = get_run_logger()
    settings = cfg["settings_obj"]
    env = EnvSettings()

    cur_cfg = settings.llm.curation
    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)

    try:
        # Fetch candidates: categorized, unpublicated articles
        candidates = fetch_candidates_for_curation(conn, limit=int(cur_cfg.batch_size))
        if not candidates:
            log.info("No categorized unpublicated articles to curate. Skipping.")
            return {"status": "skipped", "candidates": 0, "selected": 0}

        log.info("Fetched %s candidates for curation.", len(candidates))

        # Fetch recently published articles for diversity context (temporal RAG)
        recent_context = fetch_recent_published_context(
            conn, limit=int(cur_cfg.rag_context_size)
        )
        log.info(
            "Fetched %s recently published articles for context.", len(recent_context)
        )

        # Convert to plain dicts — curation module is DB-agnostic
        candidate_dicts = [
            {"id": c.id, "title": c.title, "category": c.category} for c in candidates
        ]
        context_dicts = [
            {"title": c.title, "category": c.category} for c in recent_context
        ]

        # Call LLM — raises OpenAIError or ValueError on failure (both halt the flow)
        result = curate_articles_via_openrouter(
            api_key=env.openrouter_api_key.get_secret_value(),
            model=str(settings.llm.curation_model),
            template_path=env.curation_prompt_path,
            candidates=candidate_dicts,
            recent_context=context_dicts,
            max_selected=int(cur_cfg.max_selected),
            temperature=float(cur_cfg.temperature),
            base_url=settings.llm.openrouter_base_url,
            timeout=float(settings.llm.timeout),
        )

        log.info(
            "LLM selected %s article(s) out of %s candidates.",
            len(result.selected_ids),
            len(candidates),
        )

        # Enrich selected IDs with title + category from the already-fetched candidates
        candidates_by_id = {c.id: c for c in candidates}
        articles_out = [
            {
                "id": cid,
                "title": candidates_by_id[cid].title,
                "category": candidates_by_id[cid].category,
                "link": candidates_by_id[cid].link,
            }
            for cid in result.selected_ids
        ]

        # Write to to_publish.json
        payload = {
            "curated_dt": datetime.now(timezone.utc).isoformat(),
            "articles": articles_out,
        }
        env.to_publish_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        log.info(
            "Curation complete: candidates=%d, selected=%d, output=%s",
            len(candidates),
            len(result.selected_ids),
            env.to_publish_path,
        )

        return {
            "status": "ok",
            "candidates": len(candidates),
            "selected": len(result.selected_ids),
            "output_path": str(env.to_publish_path),
        }

    except OpenAIError as exc:
        log.error("Critical LLM API failure in curation. Halting. Error: %s", str(exc))
        raise

    except ValueError as exc:
        log.error("Curation output validation failed. Halting. Error: %s", str(exc))
        raise

    finally:
        conn.close()


@task(name="task6-publish-to-telegram")
def publish_to_telegram_task(curation: dict[str, Any]) -> dict[str, Any]:
    """
    Task 6: Publish curated articles to the Telegram channel.

    Guards:
    - Skips (non-failure) if Task 5 did not run or selected 0 articles.
      This is the sole session-freshness check: task5's result is passed
      explicitly, so stale on-disk to_publish.json is never used.
    - After each successful Telegram post, immediately marks the article as
      publicated=TRUE in DB. This prevents re-selection by future Task 5 runs
      and provides idempotency even if the task is retried mid-batch.
    """
    log = get_run_logger()

    # --- Session-freshness guard -------------------------------------------
    # Only proceed if task5 produced results in *this* flow run.
    # curation["status"] is set to "ok" only when task5 actually wrote
    # to_publish.json; any other value ("skipped") means there is nothing new.
    if curation.get("status") != "ok":
        log.info(
            "Curation did not produce articles this session (status=%s). "
            "Skipping publication.",
            curation.get("status"),
        )
        return {"status": "skipped", "reason": "curation_skipped", "published": 0}

    if curation.get("selected", 0) == 0:
        log.info("Curation selected 0 articles. Skipping publication.")
        return {"status": "skipped", "reason": "zero_selected", "published": 0}

    # --- Load payload written by task5 in this session ----------------------
    env = EnvSettings()
    raw = json.loads(env.to_publish_path.read_text(encoding="utf-8"))
    raw_articles = raw.get("articles", [])

    if not raw_articles:
        log.info("to_publish.json contains no articles. Skipping publication.")
        return {"status": "skipped", "reason": "empty_payload", "published": 0}

    articles = [
        ArticleToPublish(
            id=a["id"],
            title=a["title"],
            category=a["category"],
            link=a["link"],
        )
        for a in raw_articles
    ]

    log.info(
        "Publishing %d article(s) to Telegram channel %s",
        len(articles),
        env.telegram_channel_id,
    )

    bot_token = env.telegram_bot_token.get_secret_value()
    channel_id = env.telegram_channel_id

    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)
    published_ids: list[int] = []

    try:
        for article in articles:
            msg = format_article_message(article)

            # Post to Telegram — raises TelegramPublishError on failure.
            send_telegram_message(
                bot_token=bot_token,
                channel_id=channel_id,
                text=msg,
            )

            # Mark in DB immediately after a successful post so that even if
            # the loop is interrupted, already-posted articles are not re-sent.
            mark_publicated(conn, article_id=article.id)
            conn.commit()
            published_ids.append(article.id)

            log.info(
                "Published and marked: article_id=%s category=%s title=%.80s",
                article.id,
                article.category,
                article.title,
            )

    except TelegramPublishError as exc:
        log.error(
            "Telegram publish failed after %d/%d article(s). Error: %s",
            len(published_ids),
            len(articles),
            str(exc),
        )
        raise

    finally:
        conn.close()

    log.info(
        "Publication complete: published=%d article_ids=%s",
        len(published_ids),
        published_ids,
    )
    return {
        "status": "ok",
        "published": len(published_ids),
        "article_ids": published_ids,
    }


@flow(name="daily-news-flow")
def daily_news_flow(skip_ingestion: bool = False) -> dict:
    """Main production flow entrypoint.

    Args:
        skip_ingestion: When True, tasks 2 (ingestion) and 3 (DB insert) are
            skipped entirely. Useful for running only the LLM pipeline
            (categorize → curate → publish) against the existing DB backlog,
            without spending newsdata.io credits or waiting for API calls.
            Default: False (normal full run).
    """
    cfg = load_config_task()

    if skip_ingestion:
        ingestion = {
            "status": "skipped",
            "articles": [],
            "collected_dt": None,
            "active_group": None,
            "requests_planned": 0,
            "sampled_domains": 0,
            "articles_collected": 0,
            "plan": [],
        }
        storage = {"inserted": 0, "skipped": 0, "inserted_ids": []}
    else:
        ingestion = run_ingestion_task(cfg)
        storage = insert_to_db_task(ingestion["articles"], ingestion["collected_dt"])
    categorization = categorize_backlog_task(cfg)
    curation = curate_articles_task(cfg)
    publication = publish_to_telegram_task(curation)

    return {
        "config": {
            "paths": cfg["env"],
            "settings": {
                "credits": cfg["settings_obj"].session.credits,
                "domains_per_session": cfg["settings_obj"].session.domains_per_session,
                "llm": {
                    "categorization_model": cfg[
                        "settings_obj"
                    ].llm.categorization_model,
                    "curation_model": cfg["settings_obj"].llm.curation_model,
                    "batch_size": cfg["settings_obj"].llm.categorization.batch_size,
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
        "curation": curation,
        "publication": publication,
    }


if __name__ == "__main__":
    daily_news_flow()
