"""
src/config.py

Central configuration for the AI & Robotics news curation bot.

Responsibilities:
- Read runtime configuration from environment variables (paths + secrets).
- Parse and validate config/settings.yml (YAML) into Pydantic models.
- Parse and normalize config/sources_whitelist.yml into groups -> domains mapping.
- Enforce known NewsData API constraints early (fail fast):
  - Query length limit: <= 100 characters for q/qInTitle (observed in production).

This module is intended to be used by Prefect flow modules and application modules.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


# ----------------------------
# Environment settings (docker-compose injection)
# ----------------------------


class EnvSettings(BaseSettings):
    """Environment variables (secrets + paths)."""

    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: SecretStr
    postgres_db: str
    openrouter_api_key: SecretStr
    telegram_bot_token: SecretStr
    telegram_channel_id: str
    telegram_proxy_url: str | None = None
    newsdata_api_key: SecretStr
    config_dir: Path
    data_dir: Path
    settings_path: Path
    sources_whitelist_path: Path
    last_news_path: Path
    to_publish_path: Path
    categorization_prompt_path: Path
    curation_prompt_path: Path
    analysis_prompt_path: Path = Path("/app/config/prompts/Analysis.md")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# ----------------------------
# settings.yml models (ingestion-related only)
# ----------------------------


class OrchestrationSettings(BaseModel):
    """Prefect scheduler cadence settings."""

    runs_per_day: int = Field(gt=0)
    report_interval_days: int = Field(default=7, gt=0)


class SessionSettings(BaseModel):
    """Budgeting and sampling limits per ingestion run."""

    credits: int = Field(gt=0)
    domains_per_session: int = Field(gt=0)


class NewsDataSettings(BaseModel):
    """NewsData.io parameters used by ingestion."""

    language: str
    timeframe: int | None = Field(default=None, gt=0)
    size: int = Field(gt=0)
    removeduplicate: int | bool = 1
    query_field_mode: Literal["q", "qInTitle", "random"] = "random"
    excludefield: list[str] = Field(default_factory=list)


class CategorizationSettings(BaseModel):
    """Operational knobs for categorization task."""

    batch_size: int = Field(gt=0)
    max_total_rounds: int = Field(gt=0)
    min_chunk_size: int = Field(gt=0)
    poison_mode: Literal["fail", "mark"] = "mark"
    poison_fallback_category: str = "Unrecognized"


class CurationSettings(BaseModel):
    """Operational knobs for curation task."""

    batch_size: int = Field(gt=0)
    max_selected: int = Field(gt=0)
    rag_context_size: int = Field(gt=0)
    temperature: float = Field(ge=0.0, le=1.0)


class EmbeddingSettings(BaseModel):
    """Operational knobs for embedding task."""

    dimensions: int = Field(gt=0)
    batch_size: int = Field(gt=0)


class LLMAnalysisSettings(BaseModel):
    """Operational knobs for weekly report LLM call."""

    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    timeout: float = Field(default=120.0, gt=0)
    max_output_chars: int = Field(default=3000, gt=0)


class LLMSettings(BaseModel):
    """LLM parameters used for titles categorization, curation, and embedding."""

    categorization_model: str
    curation_model: str
    embedding_model: str
    openrouter_base_url: str
    timeout: float = Field(gt=0)

    categorization: CategorizationSettings
    curation: CurationSettings
    embedding: EmbeddingSettings

    analysis_model: str = "poolside/laguna-m.1:free"
    analysis: LLMAnalysisSettings = Field(default_factory=LLMAnalysisSettings)


class ReportSettings(BaseModel):
    """Knobs for the weekly trends report pipeline."""

    max_articles_to_analyze: int = Field(default=500, gt=0)
    max_clusters: int = Field(default=8, gt=0)
    min_cluster_size: int = Field(default=3, gt=0)
    max_titles_per_cluster: int = Field(default=5, gt=0)
    max_message_chars: int = Field(default=3500, gt=0)
    include_sources_summary: bool = True
    include_vector_insights: bool = (
        True  # inject clustering signal metrics into LLM prompt
    )


class QuerySettings(BaseModel):
    """One query definition. Keep q length <= 100 to avoid API errors."""

    name: str
    q: str


class AppSettings(BaseModel):
    """
    Root schema for config/settings.yml.

    We ignore unknown fields because settings.yml may contain orchestration and other knobs
    not required at this layer.
    """

    model_config = ConfigDict(extra="ignore")

    orchestration: OrchestrationSettings
    session: SessionSettings
    newsdata: NewsDataSettings
    queries: list[QuerySettings]
    llm: LLMSettings
    report: ReportSettings = Field(default_factory=ReportSettings)


# ----------------------------
# Validators / loaders
# ----------------------------


def validate_queries_len(
    settings: AppSettings, *, max_len: int = 100, strict: bool = True
) -> None:
    """
    Validate query length constraints for NewsData.

    In your environment, NewsData returned:
      UnsupportedQueryLength: "Query length cannot be greater than 100"
    """
    offenders: list[tuple[str, int]] = []
    for q in settings.queries:
        q_len = len(q.q)
        if q_len > max_len:
            offenders.append((q.name, q_len))

    if not offenders:
        return

    msg = (
        f"NewsData query length limit exceeded (max_len={max_len}). "
        "Shorten queries in settings.yml. Offenders: "
        + ", ".join(f"{name}({length})" for name, length in offenders)
    )

    if strict:
        raise ValueError(msg)

    logger.warning(msg)


def load_settings(env: EnvSettings) -> AppSettings:
    """
    Load and validate config/settings.yml.
    """
    if not env.settings_path.exists():
        raise FileNotFoundError(f"Settings file not found: {env.settings_path}")

    raw = yaml.safe_load(env.settings_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Settings YAML must be a mapping: {env.settings_path}")

    try:
        settings = AppSettings.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid settings in {env.settings_path}: {exc}") from exc

    validate_queries_len(settings, max_len=100, strict=True)
    return settings


def load_whitelist(env: EnvSettings) -> dict[str, list[str]]:
    """
    Load config/sources_whitelist.yml and return group -> list[domain].

    Expected structure:
      groups:
        A:
          sources:
            - domain: example.com
            - domain: another.com
    """
    if not env.sources_whitelist_path.exists():
        raise FileNotFoundError(
            f"Whitelist file not found: {env.sources_whitelist_path}"
        )

    parsed = (
        yaml.safe_load(env.sources_whitelist_path.read_text(encoding="utf-8")) or {}
    )
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Whitelist YAML must be a mapping: {env.sources_whitelist_path}"
        )

    groups_block = parsed.get("groups", parsed)
    if not isinstance(groups_block, dict) or not groups_block:
        raise ValueError(
            f"Invalid groups format in whitelist: {env.sources_whitelist_path}"
        )

    groups: dict[str, list[str]] = {}
    for group_name, group_payload in groups_block.items():
        payload = group_payload or {}
        sources = payload.get("sources", [])
        if not isinstance(sources, list):
            continue

        domains: list[str] = []
        for item in sources:
            if isinstance(item, dict):
                d = str(item.get("domain", "")).strip()
                if d:
                    domains.append(d)

        # stable de-dup
        domains = list(dict.fromkeys(domains))
        if domains:
            groups[str(group_name)] = domains

    if not groups:
        raise ValueError(
            f"Whitelist file has no usable groups: {env.sources_whitelist_path}"
        )

    return groups
