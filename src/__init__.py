"""
src

Core library for the AI & Robotics news curation bot.

Modules:
    config      — Environment settings, YAML loaders, and Pydantic models.
    db          — PostgreSQL helpers (insert, categorize, curate, embed, report).
    ingestion   — NewsData.io API client and round-robin whitelist rotation.
    curation    — LLM calls for title categorization and article curation.
    embedding   — OpenRouter embeddings API wrapper.
    analysis    — Embedding clustering and weekly report generation.
    publishing  — Telegram Bot API formatter and HTTP poster.
"""
