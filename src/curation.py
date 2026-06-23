"""src/curation.py

All LLM logic lives here.

Task 4: Title categorization
- Load prompt template from an explicit template path (env-provided)
- Substitute {{TITLES_JSON}} with JSON list of titles
- Call OpenRouter using the OpenAI-compatible client
- Strictly parse and validate JSON output

Design: synchronous, no concurrency, fail-fast on malformed output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from openai import OpenAI, OpenAIError


# Keep this list in sync with config/prompts/categorization.md
PREFERRED_CATEGORIES: set[str] = {
    "Agentic AI",
    "Humanoid Robots",
    "AI Security",
    "Robotics Market",
    "AI Jobs",
    "AI Policy",
    "Health AI",
    "GenAI Research",
    "GenAI Media",
    "AI Drones",
    "Enterprise AI",
    "Open Source",
    "Hardware",
    "Science",
    "Markets",
}


@dataclass(frozen=True)
class CategorizationResult:
    titles: List[str]
    categories: List[str]
    raw_text: str


def build_categorization_prompt(*, template_path: Path, titles: Sequence[str]) -> str:
    template = template_path.read_text(encoding="utf-8")
    titles_json = json.dumps(list(titles), ensure_ascii=False)
    return template.replace("{{TITLES_JSON}}", titles_json)


def categorize_titles_via_openrouter(
    *,
    api_key: str,
    model: str,
    template_path: Path,
    titles: Sequence[str],
    temperature: float = 0.0,
    base_url: str = "https://openrouter.ai/api/v1",
) -> CategorizationResult:
    """Return exactly one primary category per title (same order)."""
    prompt = build_categorization_prompt(template_path=template_path, titles=titles)

    # TODO: Implement OpenRouter call (OpenAI-compatible) and get response text.
    raw_text = _call_openrouter(api_key=api_key, model=model, prompt=prompt, temperature=temperature)

    categories = _parse_and_validate_categories(raw_text, titles)
    return CategorizationResult(titles=list(titles), categories=categories, raw_text=raw_text)


def _call_openrouter(*, api_key: str, model: str, prompt: str, temperature: float) -> str:
    """Call OpenRouter via the OpenAI-compatible SDK and return response text."""
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
    )

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a strict JSON-only classification engine."},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
    )

    text = (resp.choices[0].message.content or "").strip()
    return text


def _parse_and_validate_categories(raw_text: str, titles: Sequence[str]) -> List[str]:
    """Parse model output and validate it is a JSON list of category strings.

    Hard requirements:
    - JSON parses successfully
    - value is list[str]
    - same length as titles
    - each category is either in PREFERRED_CATEGORIES or is a valid new 1–2 word Title Case label

    We fail-fast (raise ValueError) rather than attempting auto-repair.
    """

    try:
        parsed = json.loads(raw_text)
    except Exception as exc:
        raise ValueError(f"Categorization output is not valid JSON: {exc}. Raw: {raw_text[:500]}") from exc

    if not isinstance(parsed, list):
        raise ValueError(f"Categorization output must be a JSON array, got {type(parsed).__name__}")

    if len(parsed) != len(titles):
        raise ValueError(
            f"Categorization output length mismatch: expected {len(titles)} got {len(parsed)}. "
            f"Raw: {raw_text[:500]}"
        )

    out: List[str] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, str):
            raise ValueError(f"Category at index {i} must be a string, got {type(item).__name__}")

        cat = item.strip()
        if not cat:
            raise ValueError(f"Empty category at index {i}")

        if cat == "Other":
            raise ValueError(f"'Other' category is forbidden (index {i})")

        if len(cat.split()) > 2:
            raise ValueError(f"Category must be 1–2 words, got '{cat}' (index {i})")

        # Preferred categories always ok.
        if cat in PREFERRED_CATEGORIES:
            out.append(cat)
            continue

        # New category rules.
        if not _is_title_case_one_or_two_words(cat):
            raise ValueError(
                f"New category must be 1–2 words, letters/spaces only, Title Case. Got '{cat}' (index {i})"
            )

        out.append(cat)

    return out


def _is_title_case_one_or_two_words(s: str) -> bool:
    """
    Validate a *new* category label.

    Accepts exactly 1–2 words made of ASCII letters and a single optional space.
    Each word must be either:
      - Title Case (first letter uppercase, remaining letters lowercase), e.g. "Chinese"
      - ALL CAPS acronym, e.g. "AI", "LLM"

    This allows labels like "Chinese AI" while still rejecting non-letter characters,
    extra spaces, and lowercase labels like "chinese ai".
    """
    # Must be 1–2 words, letters/spaces only (no punctuation/digits), exactly one optional space
    if not re.fullmatch(r"^[A-Za-z]+(?: [A-Za-z]+)?$", s):
        return False

    words = s.split(" ")
    return all(w and (w.istitle() or w.isupper()) for w in words)

