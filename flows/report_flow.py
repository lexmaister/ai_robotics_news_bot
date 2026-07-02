"""
flows/report_flow.py

Prefect flow for the weekly AI & Robotics trends report and DB cleanup.

Tasks:
1) task1-load-config    — Load EnvSettings + settings.yml (same pattern as daily_news_flow).
2) task2-cleanup-db     — Delete old unpublished articles to reclaim DB/storage.
3) task3-fetch-embeddings — Fetch published article embeddings within the report window.
4) task4-generate-report  — Cluster embeddings + call LLM to produce report narrative.
5) task5-post-to-telegram — Post formatted HTML report to the Telegram channel.

Lookback window:
  All time-window logic uses orchestration.report_interval_days from settings.yml.
  The flow does NOT define its own separate lookback_days parameter to avoid redundancy.

Flow parameters (controllable from the Prefect UI or CLI):
  skip_db_clean (bool, default=False) — skip Task 2 (cleanup only).
  skip_report   (bool, default=False) — skip Tasks 3–5 (report only).

Design notes:
- Sequential execution, no concurrency.
- Fail-fast: OpenAIError or clustering errors raise and fail the flow.
- Graceful skip: an empty embeddings result skips report generation gracefully.
"""

from __future__ import annotations

import os
from typing import Any

from prefect import flow, get_run_logger, task

from src.config import EnvSettings, load_settings
from src.db import (
    DbConfig,
    connect,
    cleanup_old_unpublished,
    fetch_published_embeddings_for_report,
    EmbeddingRow,
)
from src.analysis import (
    cluster_articles,
    build_analysis_prompt,
    build_vector_stats_line,
    generate_report_via_openrouter,
)
from src.publishing import (
    TelegramPublishError,
    format_weekly_report_message,
    send_telegram_message,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_config_from_env() -> DbConfig:
    return DbConfig(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname="newsbot",
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@task(name="report-task1-load-config")
def load_config_task() -> dict[str, Any]:
    """Task 1: Load environment settings and settings.yml."""
    log = get_run_logger()
    env = EnvSettings()
    settings = load_settings(env)

    log.info(
        "Config loaded: report_interval_days=%d analysis_model=%s "
        "max_articles=%d max_clusters=%d",
        settings.orchestration.report_interval_days,
        settings.llm.analysis_model,
        settings.report.max_articles_to_analyze,
        settings.report.max_clusters,
    )
    log.info(
        "Paths: analysis_prompt=%s",
        env.analysis_prompt_path,
    )

    return {
        "env_obj": env,
        "settings_obj": settings,
    }


@task(name="report-task2-cleanup-db")
def cleanup_db_task(cfg: dict[str, Any]) -> int:
    """
    Task 2: Delete old unpublished articles.

    Uses orchestration.report_interval_days as the age threshold so that each
    weekly run clears out articles that were never published in the same window.

    Returns:
        Number of rows deleted (for logging).
    """
    log = get_run_logger()
    settings = cfg["settings_obj"]
    days = settings.orchestration.report_interval_days

    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)
    try:
        deleted = cleanup_old_unpublished(conn, older_than_days=days)
        conn.commit()
        log.info(
            "Cleanup complete: deleted %d unpublished articles older than %d days",
            deleted,
            days,
        )
        return deleted
    finally:
        conn.close()


@task(name="report-task3-fetch-embeddings")
def fetch_embeddings_task(cfg: dict[str, Any]) -> list[EmbeddingRow]:
    """
    Task 3: Fetch published article embeddings within the report window.

    Returns:
        List of EmbeddingRow (may be empty if no data available yet).
    """
    log = get_run_logger()
    settings = cfg["settings_obj"]
    days = settings.orchestration.report_interval_days
    limit = settings.report.max_articles_to_analyze

    db_cfg = _db_config_from_env()
    conn = connect(db_cfg)
    try:
        rows = fetch_published_embeddings_for_report(
            conn, lookback_days=days, limit=limit
        )
        log.info(
            "Fetched %d published articles with embeddings (last %d days, limit=%d)",
            len(rows),
            days,
            limit,
        )
        return rows
    finally:
        conn.close()


@task(name="report-task4-generate-report")
def generate_report_task(
    cfg: dict[str, Any], rows: list[EmbeddingRow]
) -> dict[str, str]:
    """
    Task 4: Cluster articles and call the LLM to produce a report narrative.

    Returns:
        Dict with keys:
          - 'report_text': LLM-generated narrative (empty string if not enough data).
          - 'vector_stats': compact clustering signal line for the Telegram post.
    """
    log = get_run_logger()
    settings = cfg["settings_obj"]
    env = cfg["env_obj"]

    empty: dict[str, str] = {"report_text": "", "vector_stats": ""}

    if not rows:
        log.warning("No embedded articles available — skipping report generation")
        return empty

    # --- Clustering ---
    clusters = cluster_articles(
        rows,
        max_clusters=settings.report.max_clusters,
        min_cluster_size=settings.report.min_cluster_size,
        max_titles_per_cluster=settings.report.max_titles_per_cluster,
    )

    if not clusters:
        log.warning(
            "No clusters passed the min_cluster_size=%d filter — skipping report",
            settings.report.min_cluster_size,
        )
        return empty

    log.info(
        "Clustering: %d articles → %d clusters",
        len(rows),
        len(clusters),
    )

    # Compact stats line appended verbatim to the Telegram post
    vector_stats = build_vector_stats_line(clusters, len(rows))
    log.info("Vector stats: %s", vector_stats)

    # --- Build prompt ---
    prompt = build_analysis_prompt(
        template_path=env.analysis_prompt_path,
        clusters=clusters,
        total_articles=len(rows),
        lookback_days=settings.orchestration.report_interval_days,
        max_output_chars=settings.llm.analysis.max_output_chars,
        include_sources_summary=settings.report.include_sources_summary,
        include_vector_insights=settings.report.include_vector_insights,
        all_rows=rows,
    )

    # --- LLM call ---
    report_text = generate_report_via_openrouter(
        api_key=env.openrouter_api_key.get_secret_value(),
        model=settings.llm.analysis_model,
        base_url=settings.llm.openrouter_base_url,
        timeout=settings.llm.analysis.timeout,
        temperature=settings.llm.analysis.temperature,
        prompt=prompt,
        max_output_chars=settings.llm.analysis.max_output_chars,
    )

    log.info("Report generated: %d chars", len(report_text))
    return {"report_text": report_text, "vector_stats": vector_stats}


@task(name="report-task5-post-to-telegram")
def post_report_task(cfg: dict[str, Any], result: dict[str, str]) -> None:
    """Task 5: Format and post the weekly report to the Telegram channel."""
    log = get_run_logger()

    report_text = result.get("report_text", "")
    vector_stats = result.get("vector_stats", "")

    if not report_text.strip():
        log.warning("Empty report text — nothing to post to Telegram")
        return

    env = cfg["env_obj"]
    settings = cfg["settings_obj"]

    message = format_weekly_report_message(
        report_text,
        max_chars=settings.report.max_message_chars,
        vector_stats=vector_stats,
    )

    log.info("Posting weekly report to Telegram (%d chars)", len(message))
    try:
        send_telegram_message(
            bot_token=env.telegram_bot_token.get_secret_value(),
            channel_id=env.telegram_channel_id,
            text=message,
            timeout=30.0,
            proxy_url=env.telegram_proxy_url,
        )
        log.info("Weekly report posted successfully")
    except TelegramPublishError as exc:
        log.error("Failed to post weekly report to Telegram: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Flow
# ---------------------------------------------------------------------------


@flow(name="report-flow")
def report_flow(
    skip_db_clean: bool = False,
    skip_report: bool = False,
) -> None:
    """
    Weekly trends report + DB cleanup flow.

    Time-window for both cleanup and report is driven by
    orchestration.report_interval_days in settings.yml.

    Parameters:
        skip_db_clean: When True, skips Task 2 (DB cleanup).  Use when you want
                       to re-run the report without touching stored articles.
        skip_report:   When True, skips Tasks 3–5 (report generation and posting).
                       Use when you want only the cleanup step.
    """
    log = get_run_logger()
    log.info(
        "report_flow started (skip_db_clean=%s, skip_report=%s)",
        skip_db_clean,
        skip_report,
    )

    cfg = load_config_task()

    deleted = 0
    if not skip_db_clean:
        deleted = cleanup_db_task(cfg)
    else:
        log.info("DB cleanup skipped (skip_db_clean=True)")

    if not skip_report:
        rows = fetch_embeddings_task(cfg)
        result = generate_report_task(cfg, rows)
        post_report_task(cfg, result)
    else:
        log.info("Report generation skipped (skip_report=True)")

    log.info("report_flow complete. articles_cleaned=%d", deleted)


if __name__ == "__main__":
    report_flow()
