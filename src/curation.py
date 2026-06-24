"""src/curation.py

All LLM logic lives here.

Task 4: Title categorization
- Load prompt template from an explicit template path (env-provided)
- Substitute {{TITLES_JSON}} with JSON list of titles
- Call OpenRouter using the OpenAI-compatible client
- Strictly parse and validate JSON output

Task 5: Article curation
- Load curation prompt template (env-provided path)
- Substitute {{CANDIDATES_JSON}}, {{RECENT_CONTEXT_JSON}}, {{MAX_SELECTED}}
- Call OpenRouter and parse a JSON array of selected article IDs
- Validate every returned ID is present in the candidate list sent to the model

Design: synchronous, no concurrency, fail-fast on malformed output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence
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
    temperature: float,
    base_url: str,
    tokens_per_title: int,
    min_tokens: int,
    timeout: float,
) -> CategorizationResult:
    """Return exactly one primary category per title (same order)."""
    prompt = build_categorization_prompt(template_path=template_path, titles=titles)

    # max_tokens scales with batch size; both knobs come from settings.yml.
    max_tokens = max(min_tokens, len(titles) * tokens_per_title)

    raw_text = _call_openrouter(
        api_key=api_key,
        model=model,
        prompt=prompt,
        temperature=temperature,
        base_url=base_url,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    categories = _parse_and_validate_categories(raw_text, titles)
    return CategorizationResult(
        titles=list(titles), categories=categories, raw_text=raw_text
    )


def _call_openrouter(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    base_url: str,
    max_tokens: int | None,
    timeout: float,
) -> str:
    """
    Call OpenRouter via the OpenAI-compatible SDK and return response text.

    stream is NOT set explicitly. Sending "stream": false in the request body
    causes some free-tier models on OpenRouter (observed: nemotron-nano) to return
    null/empty content. The SDK default is already non-streaming. If a model
    returns SSE format anyway, the SDK raises JSONDecodeError when parsing the
    response body — caught below and re-raised as OpenAIError to halt the flow.

    max_tokens caps response length to prevent network-level truncation on
    long generations from verbose models.
    """
    client = OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
    )

    create_kwargs: dict = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict JSON-only output engine. Reply with only the JSON array, no explanation.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        # NOTE: stream=False is intentionally NOT set here.
        # Explicitly sending "stream": false in the request body causes some
        # OpenRouter free-tier models (e.g. nemotron-nano) to return null content.
        # The SDK default is non-streaming; we rely on the json.JSONDecodeError
        # handler below to catch any model that returns SSE format anyway.
    }
    if max_tokens is not None:
        create_kwargs["max_tokens"] = max_tokens

    try:
        resp = client.chat.completions.create(**create_kwargs)
    except OpenAIError:
        raise
    except json.JSONDecodeError as exc:
        # The OpenAI SDK failed to parse the raw HTTP response body from OpenRouter.
        # Most common cause: OpenRouter returned SSE/streaming format on a non-streaming
        # request (observed on some :free tier models), or the response was truncated
        # by a network timeout before the JSON was complete.
        raise OpenAIError(
            f"OpenRouter HTTP response is not valid JSON "
            f"(streaming leak or truncated response): {exc}"
        ) from exc
    except Exception as exc:
        raise OpenAIError(
            f"Unexpected error calling OpenRouter: {type(exc).__name__}: {exc}"
        ) from exc

    # Guard against malformed responses from OpenRouter free-tier models.
    # Two observed failure modes when SSE/streaming leaks into a non-streaming call:
    #   1. SDK raises JSONDecodeError (caught above) — clean path.
    #   2. SDK partially parses the SSE body and produces a ChatCompletion object
    #      with choices=None or choices=[] — causes TypeError on choices[0] access.
    if not resp.choices:
        raise OpenAIError(
            f"OpenRouter returned a response with no choices "
            f"(possible SSE format leak or empty response). model={model!r}"
        )

    choice = resp.choices[0]

    # Per OpenRouter spec, NonStreamingChoice carries an optional error field
    # (code + message) for provider-level failures. Check it before content.
    choice_error = getattr(choice, "error", None)
    if choice_error:
        code = getattr(choice_error, "code", None) or getattr(
            choice_error, "get", lambda k, d=None: d
        )("code")
        msg = getattr(choice_error, "message", None) or getattr(
            choice_error, "get", lambda k, d=None: d
        )("message")
        raise OpenAIError(
            f"OpenRouter provider error in choice. "
            f"model={model!r} error_code={code!r} error_message={msg!r}"
        )

    content = choice.message.content
    finish_reason = choice.finish_reason

    if not content or not content.strip():
        # Empty/null content: model overloaded, rate-limited, content-filtered,
        # or finish_reason="length" with nothing written.
        # Raise OpenAIError to halt the flow — must NOT reach the ValueError /
        # poison-pill path, which would silently mark the backlog as "Unrecognized".
        raise OpenAIError(
            f"OpenRouter returned empty content. "
            f"model={model!r} finish_reason={finish_reason!r}"
        )

    return content.strip()


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
        raise ValueError(
            f"Categorization output is not valid JSON: {exc}. Raw: {raw_text[:500]}"
        ) from exc

    if not isinstance(parsed, list):
        raise ValueError(
            f"Categorization output must be a JSON array, got {type(parsed).__name__}"
        )

    if len(parsed) != len(titles):
        raise ValueError(
            f"Categorization output length mismatch: expected {len(titles)} got {len(parsed)}. "
            f"Raw: {raw_text[:500]}"
        )

    out: List[str] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, str):
            raise ValueError(
                f"Category at index {i} must be a string, got {type(item).__name__}"
            )

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


# ---------------------------------------------------------------------------
# Task 5: Article curation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurationResult:
    selected_ids: list[int]
    raw_text: str


def build_curation_prompt(
    *,
    template_path: Path,
    candidates: Sequence[dict],
    recent_context: Sequence[dict],
    max_selected: int,
) -> str:
    """Render the curation prompt template by substituting all placeholders."""
    template = template_path.read_text(encoding="utf-8")
    prompt = template.replace(
        "{{CANDIDATES_JSON}}", json.dumps(list(candidates), ensure_ascii=False)
    )
    prompt = prompt.replace(
        "{{RECENT_CONTEXT_JSON}}", json.dumps(list(recent_context), ensure_ascii=False)
    )
    prompt = prompt.replace("{{MAX_SELECTED}}", str(max_selected))
    return prompt


def curate_articles_via_openrouter(
    *,
    api_key: str,
    model: str,
    template_path: Path,
    candidates: Sequence[dict],
    recent_context: Sequence[dict],
    max_selected: int,
    temperature: float,
    base_url: str,
    max_tokens: int,
    timeout: float,
) -> CurationResult:
    """
    Call OpenRouter to select articles for publication.

    Candidates must be dicts with keys: "id" (int), "title" (str), "category" (str).
    Recent context must be dicts with keys: "title" (str), "category" (str).

    Raises:
        OpenAIError: API unavailable, rate-limited, or auth failure — caller should HALT.
        ValueError:  Invalid JSON output or IDs outside the candidate list — caller should HALT.
    """
    candidate_ids: set[int] = {int(c["id"]) for c in candidates}

    prompt = build_curation_prompt(
        template_path=template_path,
        candidates=candidates,
        recent_context=recent_context,
        max_selected=max_selected,
    )

    raw_text = _call_openrouter(
        api_key=api_key,
        model=model,
        prompt=prompt,
        temperature=temperature,
        base_url=base_url,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    selected_ids = _parse_and_validate_selected_ids(
        raw_text, candidate_ids=candidate_ids
    )
    return CurationResult(selected_ids=selected_ids, raw_text=raw_text)


def _parse_and_validate_selected_ids(
    raw_text: str, *, candidate_ids: set[int]
) -> list[int]:
    """
    Parse LLM output and validate it is a JSON array of integers within candidate_ids.

    Strips markdown code fences if the model wraps the output.
    Raises ValueError on any violation.
    """
    text = raw_text.strip()

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    if text.startswith("```"):
        lines = text.splitlines()
        inner = (
            lines[1:-1] if len(lines) > 2 and lines[-1].strip() == "```" else lines[1:]
        )
        text = "\n".join(inner).strip()

    try:
        parsed = json.loads(text)
    except Exception as exc:
        raise ValueError(
            f"Curation output is not valid JSON: {exc}. Raw: {raw_text[:500]}"
        ) from exc

    if not isinstance(parsed, list):
        raise ValueError(
            f"Curation output must be a JSON array, got {type(parsed).__name__}. Raw: {raw_text[:200]}"
        )

    selected: list[int] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, int):
            raise ValueError(
                f"Selected ID at index {i} must be an integer, got {type(item).__name__}: {item!r}"
            )
        if item not in candidate_ids:
            raise ValueError(
                f"Selected ID {item} (index {i}) is not in the candidate list sent to the model."
            )
        selected.append(item)

    return selected
