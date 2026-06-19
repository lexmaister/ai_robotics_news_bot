"""
src/ingestion.py

NewsData.io ingestion for the AI & Robotics news curation pipeline.

This module is Prefect-agnostic: it defines no flows/tasks and does not configure logging.
Prefect flows should:
- load env/settings/whitelist via src.config
- call run_ingestion(settings=..., whitelist_groups=..., last_news_path=..., newsdata_api_key=...)

State:
- The only persisted state is last_news.json (round-robin whitelist group rotation + last payload).

Notes:
- Query length constraints are validated in src.config.validate_queries_len().
- NewsData 'sort' is intentionally not used; API default sorting is relied upon.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from typing import Any, Literal, TypedDict

from newsdataapi import NewsDataApiClient, NewsdataRateLimitError

from src.config import AppSettings  # single source of truth for settings types

logger = logging.getLogger(__name__)


# ----------------------------
# last_news.json persistence
# ----------------------------

class LastNews:
    """
    Persisted ingestion state used for round-robin rotation.

    Stored JSON schema:
      {
        "articles": [...],
        "request_params": {...},
        "domain_group": "A",
        "collected_dt": "2026-06-17T14:33:54.720554+00:00"
      }
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._articles: list[dict[str, Any]] = []
        self._request_params: dict[str, Any] = {}
        self._domain_group: str = "A"
        self._collected_dt: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            logger.warning("last_news missing: %s (defaulting domain_group=%s)", self.path, "A")
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Last news file is not valid JSON: {self.path}") from exc

        self._articles = data.get("articles", []) or []
        self._request_params = data.get("request_params", {}) or {}
        self._domain_group = data.get("domain_group") or "A"
        self._collected_dt = data.get("collected_dt")

        logger.info(
            "last_news loaded: file=%s articles=%s domain_group=%s collected_dt=%s",
            self.path.name,
            len(self._articles),
            self._domain_group,
            self._collected_dt,
        )

    def _save(self) -> None:
        parent = self.path.parent
        if not parent.exists():
            raise FileNotFoundError(
                f"LastNews save directory does not exist: {parent}. "
                "Check Docker volume mapping for /app/data."
            )
        if not parent.is_dir():
            raise NotADirectoryError(f"LastNews save parent is not a directory: {parent}")

        payload = {
            "articles": self._articles,
            "request_params": self._request_params,
            "domain_group": self._domain_group,
            "collected_dt": self._collected_dt,
        }

        try:
            self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except PermissionError as exc:
            raise PermissionError(
                f"LastNews file is not writable: {self.path}. "
                "Check that /app/data is writable and not mounted read-only."
            ) from exc

        logger.info(
            "last_news saved: file=%s articles=%s domain_group=%s collected_dt=%s",
            self.path.name,
            len(self._articles),
            self._domain_group,
            self._collected_dt,
        )

    @property
    def domain_group(self) -> str:
        return self._domain_group

    @property
    def collected_dt(self) -> str | None:
        return self._collected_dt

    def update(self, *, articles: list[dict[str, Any]], request_params: dict[str, Any], domain_group: str) -> None:
        """Update persisted state and write last_news.json (UTC timestamp)."""
        self._articles = articles
        self._request_params = request_params
        self._domain_group = domain_group
        self._collected_dt = datetime.now(timezone.utc).isoformat()
        self._save()


# ----------------------------
# ingestion core
# ----------------------------

class RequestMeta(TypedDict):
    group: str
    domain: str
    query_name: str
    query_field: Literal["q", "qInTitle"]
    params: dict[str, Any]


@dataclass(frozen=True)
class IngestionResult:
    """
    Result returned to Prefect.

    `status` is intended for branching:
    - "ok": requests were executed
    - "no_plan": chosen group had no domains; no API calls were made
    """
    status: Literal["ok", "no_plan"]
    active_group: str
    requests_planned: int
    sampled_domains: int
    articles_collected: int
    collected_dt: str
    plan: list[RequestMeta]
    articles: list[dict[str, Any]]


class NewsDataManager:
    """
    Builds request plans and fetches articles from NewsData.io.

    Rotation:
    - Uses sorted whitelist group names and `last_group` from last_news.json.
    - Selects next group in round-robin order.
    """

    def __init__(
        self,
        *,
        settings: AppSettings,
        api_key: str,
        whitelist_groups: dict[str, list[str]],
        last_group: str,
        rng: Random | None = None,
    ) -> None:
        self.settings = settings
        self.api_key = api_key
        self.rng = rng or Random()

        group_names = sorted(whitelist_groups.keys())
        if not group_names:
            raise ValueError("whitelist_groups is empty")

        next_idx = (group_names.index(last_group) + 1) % len(group_names) if last_group in group_names else 0
        self.active_group = group_names[next_idx]
        self.active_domains = whitelist_groups.get(self.active_group, [])

    def build_request_plan(self) -> tuple[list[RequestMeta], int]:
        """Build (plan, sampled_domains_count), truncating plan to session credits."""
        domains = self.active_domains
        if not domains:
            return [], 0

        ns = self.settings.newsdata
        base_params: dict[str, Any] = {
            "language": ns.language,
            "size": ns.size,
            "removeduplicate": ns.removeduplicate,
        }
        if ns.timeframe is not None:
            base_params["timeframe"] = ns.timeframe
        if ns.excludefield:
            base_params["excludefield"] = ",".join(ns.excludefield)

        sampled_domains = self.rng.sample(domains, k=min(self.settings.session.domains_per_session, len(domains)))

        plan: list[RequestMeta] = []
        for domain in sampled_domains:
            for q in self.settings.queries:
                field: Literal["q", "qInTitle"] = (
                    self.rng.choice(["q", "qInTitle"])
                    if ns.query_field_mode == "random"
                    else ns.query_field_mode  # type: ignore[assignment]
                )

                plan.append(
                    {
                        "group": self.active_group,
                        "domain": domain,
                        "query_name": q.name,
                        "query_field": field,
                        "params": {**base_params, "domainurl": domain, field: q.q},
                    }
                )

        return plan[: self.settings.session.credits], len(sampled_domains)

    def normalize_article(self, article: dict[str, Any], meta: RequestMeta) -> dict[str, Any]:
        """Normalize API result and attach request metadata, respecting exclusions."""
        excluded = set(self.settings.newsdata.excludefield)
        
        # Complete list of possible API fields
        api_fields = [
            "article_id", "title", "description", "link", "pubDate",
            "source_id", "source_name", "category", "country", "language",
            "keywords", "creator", "image_url", "video_url", "source_icon"
        ]
        
        # Extract only the API fields that are NOT in the excludefield list
        keys_to_extract = [k for k in api_fields if k not in excluded]
        normalized = {k: article.get(k) for k in keys_to_extract}
        
        # Attach internal metadata
        normalized["request_domain"] = meta["domain"]
        normalized["request_query_name"] = meta["query_name"]
        normalized["request_query_field"] = meta["query_field"]
            
        return normalized

    def fetch_articles(self, plan: list[RequestMeta]) -> list[dict[str, Any]]:
        """Execute the request plan (graceful stop on 429 Rate Limit, fail-fast on others)."""
        if not plan:
            return []

        articles: list[dict[str, Any]] = []
        with NewsDataApiClient(apikey=self.api_key) as client:
            total = len(plan)
            for i, meta in enumerate(plan, start=1):
                logger.info(
                    "NewsData request %s/%s | group=%s domain=%s query=%s field=%s",
                    i,
                    total,
                    meta["group"],
                    meta["domain"],
                    meta["query_name"],
                    meta["query_field"],
                )
                try:
                    resp = client.latest_api(**meta["params"])
                    results = resp.get("results") or []
                    articles.extend(self.normalize_article(a, meta) for a in results)
                    
                except NewsdataRateLimitError as e:
                    # Gracefully catch the 429 error (limit exceeded / out of credits)
                    logger.warning(
                        "NewsData API limit reached on request %s/%s. Stopping gracefully and saving %s collected articles. | retry_after=%s",
                        i,
                        total,
                        len(articles),
                        getattr(e, 'retry_after', 'unknown')
                    )
                    break  # Exit the loop, but DO NOT raise. Return what we have so far.
                    
                except Exception:
                    # Fail-fast on unexpected errors (e.g., 500 Server Error, Network Error)
                    logger.exception(
                        "NewsData request failed | group=%s domain=%s query=%s field=%s",
                        meta["group"],
                        meta["domain"],
                        meta["query_name"],
                        meta["query_field"],
                    )
                    raise

        return articles


def run_ingestion(
    *,
    settings: AppSettings,
    whitelist_groups: dict[str, list[str]],
    last_news_path: Path,
    newsdata_api_key: str,
    rng: Random | None = None,
) -> IngestionResult:
    """
    Stateless entry point for Prefect flows (all configuration is passed in).

    Args:
        settings: Validated AppSettings from src.config.load_settings(env).
        whitelist_groups: Parsed whitelist from src.config.load_whitelist(env).
        last_news_path: Path to /app/data/last_news.json (persistent state).
        newsdata_api_key: API key string (SecretStr already unwrapped).
        rng: Optional Random instance (useful for deterministic tests).

    Returns:
        IngestionResult suitable for Prefect branching and downstream steps.
    """
    last_news = LastNews(last_news_path)

    manager = NewsDataManager(
        settings=settings,
        api_key=newsdata_api_key,
        whitelist_groups=whitelist_groups,
        last_group=last_news.domain_group,
        rng=rng,
    )

    plan, sampled_domains = manager.build_request_plan()
    if not plan:
        now = datetime.now(timezone.utc).isoformat()
        logger.warning(
            "No request plan built | group=%s domains_in_group=%s",
            manager.active_group,
            len(manager.active_domains),
        )
        # Intentionally do not update last_news on no_plan.
        return IngestionResult(
            status="no_plan",
            active_group=manager.active_group,
            requests_planned=0,
            sampled_domains=0,
            articles_collected=0,
            collected_dt=now,
            plan=[],
            articles=[],
        )

    articles = manager.fetch_articles(plan)

    last_news.update(
        articles=articles,
        request_params={
            "requests_planned": len(plan),
            "sampled_domains": sampled_domains,
            "domains_in_group": len(manager.active_domains),
        },
        domain_group=manager.active_group,
    )

    logger.info(
        "Ingestion completed | status=ok group=%s sampled_domains=%s requests=%s articles=%s",
        manager.active_group,
        sampled_domains,
        len(plan),
        len(articles),
    )

    return IngestionResult(
        status="ok",
        active_group=manager.active_group,
        requests_planned=len(plan),
        sampled_domains=sampled_domains,
        articles_collected=len(articles),
        collected_dt=last_news.collected_dt or datetime.now(timezone.utc).isoformat(),
        plan=plan,
        articles=articles,
    )
