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

from openai import OpenAI, OpenAIError

logger = logging.getLogger(__name__)


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

    Args:
        api_key:    OpenRouter API key.
        model:      Model identifier, e.g. "nvidia/llama-nemotron-embed-vl-1b-v2:free".
        titles:     Titles to embed (non-empty).
        dimensions: Requested output dimension (must match the DB VECTOR column).
        base_url:   OpenRouter API base URL.
        timeout:    HTTP request timeout in seconds.

    Returns:
        List of float vectors, one per title, in the same order as `titles`.

    Raises:
        openai.OpenAIError: on any API-level failure (network, auth, rate limit).
        ValueError: if the API returns a different number of vectors than titles.
    """
    if not titles:
        return []

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    response = client.embeddings.create(
        model=model,
        input=titles,
        dimensions=dimensions,
    )

    # Sort by index to guarantee result order matches input order.
    sorted_data = sorted(response.data, key=lambda e: e.index)

    if len(sorted_data) != len(titles):
        raise ValueError(
            f"Embedding API returned {len(sorted_data)} vectors for {len(titles)} titles."
        )

    vectors = [e.embedding for e in sorted_data]

    # Validate each vector has the expected dimension.
    for i, vec in enumerate(vectors):
        if len(vec) != dimensions:
            raise ValueError(
                f"Vector at index {i} has {len(vec)} dimensions, expected {dimensions}. "
                "Check that the model supports the requested `dimensions` parameter."
            )

    logger.debug(
        "Embedded %s titles with model=%s dimensions=%s", len(titles), model, dimensions
    )
    return vectors
