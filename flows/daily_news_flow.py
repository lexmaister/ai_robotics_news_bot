"""
flows/daily_news_flow.py

Production flow entrypoint (config smoke test stage).

This flow verifies that:
- EnvSettings are present (paths + secrets injected by docker-compose/Prefect).
- settings.yml parses and validates (including query length <= 100).
- sources_whitelist.yml parses into groups -> domains.

It intentionally stops before ingestion to allow safe validation in Prefect runs.
"""

from __future__ import annotations

from prefect import flow, get_run_logger

from src.config import EnvSettings, load_settings, load_whitelist


@flow(name="daily-news-flow")
def daily_news_flow() -> dict:
    """
    Load and validate runtime config.

    Returns a JSON-serializable dict so Prefect UI can display it and downstream
    tasks can consume it later (when ingestion is enabled).
    """
    log = get_run_logger()

    # 1) Load env configuration injected by docker-compose / Prefect worker env
    env = EnvSettings()

    # 2) Load and validate settings.yml (includes validate_queries_len strict check)
    settings = load_settings(env)

    # 3) Load whitelist groups/domains
    whitelist = load_whitelist(env)

    # 4) Log a compact summary (this is what you want to see in Prefect UI)
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

    # Stop here on purpose: no ingestion yet
    return {
        "ok": True,
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
    }


if __name__ == "__main__":
    daily_news_flow()