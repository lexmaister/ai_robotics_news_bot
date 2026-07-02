"""
src/analysis.py

Weekly report analysis pipeline for the AI & Robotics news bot.

Responsibilities:
- Cluster published article embeddings using scikit-learn KMeans / MiniBatchKMeans.
- Build a structured "trend payload" (clusters: size, top categories, representative titles).
- Call OpenRouter LLM (llm.analysis_model) to convert the payload into a concise
  Telegram-ready weekly report narrative.
- Prompt template is loaded from config/prompts/Analysis.md.

Design notes:
- Clustering is L2-normalised before KMeans so that Euclidean distance approximates
  cosine similarity (standard practice with unit-norm embeddings).
- MiniBatchKMeans is used for n >= 500 articles to avoid slow convergence on large sets.
- All OpenRouter calls follow the same OpenAI-compatible client pattern as curation.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
from numpy import ndarray
from sklearn.cluster import KMeans, MiniBatchKMeans
from sklearn.preprocessing import normalize
from openai import OpenAI, OpenAIError

from src.db import EmbeddingRow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ClusterSummary:
    """Summary of one topic cluster produced by the embedding clustering step."""

    cluster_id: int
    size: int
    top_categories: list[str]
    representative_titles: list[str]  # articles closest to centroid (most typical)
    cohesion: float = 0.0  # mean distance to centroid; lower = tighter cluster
    outlier_titles: list[str] = field(
        default_factory=list
    )  # articles farthest from centroid
    top_domain: Optional[str] = None


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cluster_articles(
    rows: list[EmbeddingRow],
    *,
    max_clusters: int,
    min_cluster_size: int,
    max_titles_per_cluster: int,
) -> list[ClusterSummary]:
    """
    Cluster article embedding vectors using KMeans (small datasets) or
    MiniBatchKMeans (large datasets).

    Args:
        rows:                  Articles with pre-computed embeddings.
        max_clusters:          Maximum k for KMeans (actual clusters may be less
                               after applying min_cluster_size filter).
        min_cluster_size:      Clusters with fewer articles than this are excluded.
        max_titles_per_cluster: Max representative titles per cluster, chosen as
                               the articles geometrically closest to the centroid.

    Returns:
        List of ClusterSummary sorted by size descending.  Empty if no rows or
        all clusters fall below min_cluster_size.
    """
    if not rows:
        logger.warning("cluster_articles: no rows supplied — returning empty list")
        return []

    n = len(rows)
    k = min(max_clusters, n)

    # Degenerate case: single effective cluster
    if k < 2:
        cats: dict[str, int] = {}
        for r in rows:
            c = r.category or "Unknown"
            cats[c] = cats.get(c, 0) + 1
        top_cats = sorted(cats, key=lambda c: -cats[c])[:3]
        return [
            ClusterSummary(
                cluster_id=0,
                size=n,
                top_categories=top_cats,
                representative_titles=[r.title for r in rows[:max_titles_per_cluster]],
            )
        ]

    # Build embedding matrix; L2-normalise so Euclidean ≈ cosine distance
    matrix: ndarray = np.array([r.embedding for r in rows], dtype=np.float32)
    matrix = normalize(matrix)

    # Choose algorithm by data volume for runtime efficiency
    if n >= 500:
        model = MiniBatchKMeans(n_clusters=k, random_state=42, n_init=3, batch_size=256)
    else:
        model = KMeans(n_clusters=k, random_state=42, n_init=10)

    labels: ndarray = model.fit_predict(matrix)

    summaries: list[ClusterSummary] = []
    for cid in range(k):
        indices = [i for i in range(n) if labels[i] == cid]
        if len(indices) < min_cluster_size:
            logger.debug(
                "Cluster %d has %d articles (< min_cluster_size=%d), skipping",
                cid,
                len(indices),
                min_cluster_size,
            )
            continue

        cluster_rows = [rows[i] for i in indices]

        # Top categories by frequency
        cat_counts: dict[str, int] = {}
        for r in cluster_rows:
            cat = r.category or "Unknown"
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        top_cats = sorted(cat_counts, key=lambda c: -cat_counts[c])[:3]

        # Representative titles: closest to centroid (geometrically most "central")
        centroid = model.cluster_centers_[cid]
        cluster_matrix = matrix[np.array(indices)]
        dists = np.linalg.norm(cluster_matrix - centroid, axis=1)
        sorted_local_idx = np.argsort(dists)
        top_n = min(max_titles_per_cluster, len(cluster_rows))
        rep_titles = [cluster_rows[int(i)].title for i in sorted_local_idx[:top_n]]

        # Cohesion: mean distance of cluster articles to centroid (lower = tighter)
        cohesion = float(np.mean(dists))

        # Outlier titles: articles farthest from centroid (niche/unique angle within theme)
        outlier_titles = [
            cluster_rows[int(i)].title
            for i in sorted_local_idx[-top_n:][::-1]
            if cluster_rows[int(i)].title not in rep_titles
        ][:top_n]

        # Top source domain (optional source attribution)
        domain_counts: dict[str, int] = {}
        for r in cluster_rows:
            if r.request_domain:
                domain_counts[r.request_domain] = (
                    domain_counts.get(r.request_domain, 0) + 1
                )
        top_domain = (
            max(domain_counts, key=lambda d: domain_counts[d])
            if domain_counts
            else None
        )

        summaries.append(
            ClusterSummary(
                cluster_id=cid,
                size=len(cluster_rows),
                top_categories=top_cats,
                representative_titles=rep_titles,
                cohesion=cohesion,
                outlier_titles=outlier_titles,
                top_domain=top_domain,
            )
        )

    summaries.sort(key=lambda c: -c.size)
    logger.info(
        "Clustering complete: %d articles → %d clusters (k=%d, min_size=%d)",
        n,
        len(summaries),
        k,
        min_cluster_size,
    )
    return summaries


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _compute_vector_insights(
    clusters: list[ClusterSummary], total_articles: int
) -> str:
    """
    Derive three signal metrics from clustering results for the LLM prompt.

    Variant A — Coverage breadth: number of distinct clusters → broad/moderate/focused.
    Variant B — Dominant trend share: top cluster's article count as % of total.
    Variant C — Most focused cluster: lowest cohesion score (tightest, most homogeneous).

    These give the LLM real signal to write a stronger, data-grounded intro sentence.
    If you want to expose additional metrics, compute them here and append to `lines`.
    """
    if not clusters:
        return ""

    # Variant A: coverage breadth
    n_clusters = len(clusters)
    breadth = (
        "broad" if n_clusters >= 6 else "moderate" if n_clusters >= 4 else "focused"
    )

    # Variant B: dominant trend share
    top = clusters[0]
    top_pct = round(top.size / total_articles * 100) if total_articles > 0 else 0
    top_cat = top.top_categories[0] if top.top_categories else "—"

    # Variant C: most focused cluster (smallest mean centroid distance)
    most_focused = min(clusters, key=lambda c: c.cohesion)
    focused_cat = most_focused.top_categories[0] if most_focused.top_categories else "—"

    lines = [
        "Signal metrics (use to strengthen your intro sentence):",
        f"• Coverage breadth: {n_clusters} distinct clusters — {breadth} week.",
        f'• Dominant trend: "{top_cat}" leads with {top_pct}% of articles ({top.size}/{total_articles}).',
        f'• Most focused cluster: "{focused_cat}" (cohesion {most_focused.cohesion:.2f} — articles are tightly related).',
    ]
    return "\n".join(lines)


def build_analysis_prompt(
    *,
    template_path: Path,
    clusters: list[ClusterSummary],
    total_articles: int,
    lookback_days: int,
    max_output_chars: int,
    include_sources_summary: bool,
    include_vector_insights: bool,
    all_rows: list[EmbeddingRow],
) -> str:
    """
    Render the Analysis.md prompt template with cluster data and metadata.

    Substituted placeholders:
        {{CLUSTERS_JSON}}         — JSON array of cluster objects (includes cohesion + outlier_titles)
        {{DATE_RANGE}}            — human-readable date span, e.g. "Jun 25 – Jul 2, 2026"
        {{TOTAL_ARTICLES}}        — integer
        {{CLUSTER_COUNT}}         — integer
        {{SOURCE_BLOCK}}          — optional top-5 sources block (or empty string)
        {{VECTOR_INSIGHTS_BLOCK}} — optional signal metrics block (or empty string)
        {{MAX_OUTPUT_CHARS}}      — integer character limit for LLM output
    """
    template = template_path.read_text(encoding="utf-8")

    # Compute date range string from run date and lookback window
    today = date.today()
    date_from = today - timedelta(days=lookback_days - 1)
    date_to = today

    def _fmt(d: date) -> str:
        return f"{d.strftime('%b')} {d.day}"

    if date_from.year == date_to.year:
        date_range = f"{_fmt(date_from)} – {_fmt(date_to)}, {date_to.year}"
    else:
        date_range = (
            f"{_fmt(date_from)}, {date_from.year} – {_fmt(date_to)}, {date_to.year}"
        )

    # Cluster JSON: include cohesion + outlier_titles so LLM can use niche angles
    clusters_data = [
        {
            "cluster": idx + 1,
            "size": c.size,
            "top_categories": c.top_categories,
            "representative_titles": c.representative_titles,
            "outlier_titles": c.outlier_titles,
            "cohesion": round(c.cohesion, 3),
        }
        for idx, c in enumerate(clusters)
    ]
    clusters_json = json.dumps(clusters_data, ensure_ascii=False, indent=2)

    # Optional vector insights block
    vector_insights_block = (
        _compute_vector_insights(clusters, total_articles)
        if include_vector_insights
        else ""
    )

    # Optional sources block
    source_block = ""
    if include_sources_summary:
        domain_counts: dict[str, int] = {}
        for r in all_rows:
            if r.request_domain:
                domain_counts[r.request_domain] = (
                    domain_counts.get(r.request_domain, 0) + 1
                )
        if domain_counts:
            top_domains = sorted(domain_counts, key=lambda d: -domain_counts[d])[:5]
            lines = ["## Top Sources This Week"] + [
                f"- {d} ({domain_counts[d]} articles)" for d in top_domains
            ]
            source_block = "\n".join(lines)

    prompt = (
        template.replace("{{CLUSTERS_JSON}}", clusters_json)
        .replace("{{DATE_RANGE}}", date_range)
        .replace("{{TOTAL_ARTICLES}}", str(total_articles))
        .replace("{{CLUSTER_COUNT}}", str(len(clusters)))
        .replace("{{VECTOR_INSIGHTS_BLOCK}}", vector_insights_block)
        .replace("{{SOURCE_BLOCK}}", source_block)
        .replace("{{MAX_OUTPUT_CHARS}}", str(max_output_chars))
    )
    return prompt


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def generate_report_via_openrouter(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout: float,
    temperature: float,
    prompt: str,
    max_output_chars: int,
) -> str:
    """
    Call OpenRouter to generate the weekly report narrative from the cluster payload.

    Uses the OpenAI-compatible client (same pattern as curation.py / embedding.py).
    Output is trimmed at the last newline boundary if it exceeds max_output_chars.

    Args:
        api_key:         OpenRouter API key.
        model:           Model identifier (e.g. "poolside/laguna-m.1:free").
        base_url:        OpenRouter API base URL.
        timeout:         HTTP request timeout in seconds.
        temperature:     LLM sampling temperature (0.0 = deterministic).
        prompt:          Rendered Analysis.md prompt.
        max_output_chars: Hard cap applied in Python; LLM output may be longer.

    Returns:
        Report text string (at most max_output_chars characters).

    Raises:
        openai.OpenAIError: on any API-level failure.
    """
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a concise editorial AI writing weekly newsletter digests "
                    "for a professional AI & Robotics audience."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
    )

    if not resp.choices:
        raise OpenAIError(
            f"OpenRouter returned a response with no choices. model={model!r}"
        )

    choice = resp.choices[0]
    choice_error = getattr(choice, "error", None)
    if choice_error:
        code = getattr(choice_error, "code", None) or (
            choice_error.get("code") if isinstance(choice_error, dict) else None
        )
        msg = getattr(choice_error, "message", None) or (
            choice_error.get("message") if isinstance(choice_error, dict) else None
        )
        raise OpenAIError(
            f"OpenRouter provider error. "
            f"model={model!r} error_code={code!r} error_message={msg!r}"
        )

    content = choice.message.content
    if not content or not content.strip():
        raise OpenAIError(
            f"OpenRouter returned empty content. "
            f"model={model!r} finish_reason={choice.finish_reason!r}"
        )

    text = content.strip()
    if len(text) > max_output_chars:
        logger.warning(
            "Report text truncated: %d → %d chars", len(text), max_output_chars
        )
        # Trim at last newline boundary to avoid cutting mid-sentence
        trimmed = text[:max_output_chars]
        last_nl = trimmed.rfind("\n")
        text = trimmed[:last_nl].rstrip() if last_nl > 0 else trimmed

    return text
