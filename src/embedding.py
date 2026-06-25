"""
src/embedding.py

Embedding module for the AI & Robotics news curation bot.

Responsibilities:
- Call OpenRouter embeddings API (OpenAI-compatible /embeddings endpoint) to
  produce title vectors.
- Return raw float vectors in input order; DB writes are handled by the caller.

Model: nvidia/llama-nemotron-embed-vl-1b-v2:free (via OpenRouter)
Output dimension: 1536 (must match VECTOR(1536) column in db/schema.sql).
"""

from __future__ import annotations

import logging
import math

from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)


def _fit_to_dim(vec: list[float], target: int) -> list[float]:
    """
    Fit `vec` to exactly `target` dimensions.

    - If len(vec) == target: return as-is.
    - If len(vec) > target: truncate to target, then L2-normalize
      (standard Matryoshka Representation Learning approximation).
    - If len(vec) < target: raise ValueError — padding would corrupt semantics.
    """
    if len(vec) == target:
        return vec
    if len(vec) < target:
        raise ValueError(
            f"Model returned {len(vec)}-dim vector but DB requires {target} dims. "
            "Choose a model whose native output dimension is >= the target."
        )
    # Truncate + L2-normalize.
    truncated = vec[:target]
    norm = math.sqrt(sum(x * x for x in truncated))
    if norm == 0.0:
        return truncated
    return [x / norm for x in truncated]


def embed_titles_via_openrouter(
    *,
    api_key: str,
    model: str,
    titles: list[str],
    dimensions: int,
    base_url: str,
    timeout: float,
) -> list[list[float]]:
    """
    Embed a list of article titles via the OpenRouter embeddings API.

    The `dimensions` parameter is NOT forwarded to the API because many models
    served via OpenRouter do not support it and return an error.  Instead, the
    returned vectors are fitted to `dimensions` in Python:
    - exact match → used directly
    - native dim > target → truncate + L2-normalize (MRL approximation)
    - native dim < target → ValueError (cannot pad meaningfully)

    Args:
        api_key:    OpenRouter API key.
        model:      Model identifier, e.g. "nvidia/llama-nemotron-embed-vl-1b-v2:free".
        titles:     Titles to embed (non-empty).
        dimensions: Required output dimension (must match the DB VECTOR column).
        base_url:   OpenRouter API base URL.
        timeout:    HTTP request timeout in seconds.

    Returns:
        List of float vectors of length `dimensions`, one per title, in input order.

    Raises:
        openai.OpenAIError: on any API-level failure (network, auth, rate limit).
        ValueError: if the API returns an unexpected number of vectors or if the
                    native dimension is smaller than `dimensions`.
    """
    if not titles:
        return []

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    # `dimensions` is intentionally omitted — not universally supported by OpenRouter models.
    response = client.embeddings.create(
        model=model,
        input=titles,
    )

    # Sort by index to guarantee result order matches input order.
    sorted_data = sorted(response.data, key=lambda e: e.index)

    if len(sorted_data) != len(titles):
        raise ValueError(
            f"Embedding API returned {len(sorted_data)} vectors for {len(titles)} titles."
        )

    vectors = []
    for i, item in enumerate(sorted_data):
        vec = _fit_to_dim(item.embedding, dimensions)
        vectors.append(vec)

    logger.debug(
        "Embedded %s titles with model=%s native_dim=%s target_dim=%s",
        len(titles),
        model,
        len(sorted_data[0].embedding) if sorted_data else 0,
        dimensions,
    )
    return vectors
