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
- Call OpenRouter using function/tool calling (select_articles tool)
- Model fills typed {"selected_ids": [...]} arguments — no free-text parsing needed
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
    """Parsed output of a single categorization LLM call."""

    titles: List[str]
    categories: List[str]
    raw_text: str


def build_categorization_prompt(*, template_path: Path, titles: Sequence[str]) -> str:
    """Render the categorization prompt template with the given titles JSON."""
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
    timeout: float,
) -> CategorizationResult:
    """Return exactly one primary category per title (same order)."""
    prompt = build_categorization_prompt(template_path=template_path, titles=titles)

    raw_text = _call_openrouter(
        api_key=api_key,
        model=model,
        prompt=prompt,
        temperature=temperature,
        base_url=base_url,
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
    timeout: float,
) -> str:
    """
    Call OpenRouter via the OpenAI-compatible SDK (non-streaming) and return response text.

    Used exclusively for categorization (nemotron-nano). This model returns a
    well-formed non-streaming JSON response with no SSE leakage.
    No max_tokens is set — the output is a small JSON array and the model
    handles it correctly without a cap.
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
                "content": "You are a strict JSON-only classification engine.",
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

# Tool definition used to force structured output from the curation model.
# Tool calling bypasses free-text generation entirely — the model fills in
# typed function arguments, so preamble text, reasoning traces, and SSE
# format issues are all irrelevant. The response is always a structured dict.
_SELECT_ARTICLES_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "select_articles",
        "description": "Select the IDs of the best articles to publish to the Telegram channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "selected_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "IDs of the selected articles, in any order.",
                },
            },
            "required": ["selected_ids"],
            "additionalProperties": False,
        },
    },
}


def _call_openrouter_tool(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    base_url: str,
    tool: dict,
    timeout: float,
) -> str:
    """
    Call OpenRouter using function/tool calling and return the raw tool-argument JSON.

    Tool calling forces the model to emit structured JSON arguments directly,
    bypassing free-text generation entirely. This eliminates preamble text,
    SSE format issues, and reasoning traces that large models may prepend before
    the actual output. The response is always a well-formed JSON object matching
    the declared tool schema.

    Returns the raw arguments JSON string, e.g. '{"selected_ids": [42, 17, 93]}'.
    """
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
    tool_name = tool["function"]["name"]

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": tool_name}},
            stream=True,
        )

        args_parts: list[str] = []
        finish_reason: str | None = None
        choice_error = None

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]

            choice_error = getattr(choice, "error", None)
            if choice_error:
                break

            if choice.finish_reason:
                finish_reason = choice.finish_reason

            if choice.delta.tool_calls:
                for tc in choice.delta.tool_calls:
                    if tc.function and tc.function.arguments:
                        args_parts.append(tc.function.arguments)

    except OpenAIError:
        raise
    except Exception as exc:
        raise OpenAIError(
            f"Unexpected error calling OpenRouter with tool: {type(exc).__name__}: {exc}"
        ) from exc

    if choice_error:
        code = getattr(choice_error, "code", None) or (
            choice_error.get("code") if isinstance(choice_error, dict) else None
        )
        msg = getattr(choice_error, "message", None) or (
            choice_error.get("message") if isinstance(choice_error, dict) else None
        )
        raise OpenAIError(
            f"OpenRouter provider error in tool streaming chunk. "
            f"model={model!r} error_code={code!r} error_message={msg!r}"
        )

    args_json = "".join(args_parts).strip()
    if not args_json:
        raise OpenAIError(
            f"OpenRouter returned empty tool call arguments. "
            f"model={model!r} finish_reason={finish_reason!r}"
        )

    return args_json


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
    timeout: float,
) -> CurationResult:
    """
    Call OpenRouter to select articles for publication via tool/function calling.

    Using tool calling instead of plain text generation ensures the model always
    returns a typed {"selected_ids": [...]} object regardless of model size or
    reasoning verbosity — no preamble stripping or regex extraction needed.

    Candidates must be dicts with keys: "id" (int), "title" (str), "category" (str).
    Recent context must be dicts with keys: "title" (str), "category" (str).

    Raises:
        OpenAIError: API unavailable, rate-limited, or auth failure — caller should HALT.
        ValueError:  Invalid tool output or IDs outside the candidate list — caller should HALT.
    """
    candidate_ids: set[int] = {int(c["id"]) for c in candidates}

    prompt = build_curation_prompt(
        template_path=template_path,
        candidates=candidates,
        recent_context=recent_context,
        max_selected=max_selected,
    )

    raw_args = _call_openrouter_tool(
        api_key=api_key,
        model=model,
        prompt=prompt,
        temperature=temperature,
        base_url=base_url,
        tool=_SELECT_ARTICLES_TOOL,
        timeout=timeout,
    )

    selected_ids = _parse_and_validate_selected_ids(
        raw_args, candidate_ids=candidate_ids
    )
    return CurationResult(selected_ids=selected_ids, raw_text=raw_args)


def _parse_and_validate_selected_ids(
    raw_args: str, *, candidate_ids: set[int]
) -> list[int]:
    """
    Parse select_articles tool call arguments and validate all IDs.

    Expected input (from _call_openrouter_tool):
        '{"selected_ids": [42, 17, 93]}'

    Raises ValueError on any structural or semantic violation.
    """
    try:
        args = json.loads(raw_args)
    except Exception as exc:
        raise ValueError(
            f"Tool call arguments are not valid JSON: {exc}. Raw: {raw_args[:500]}"
        ) from exc

    if not isinstance(args, dict):
        raise ValueError(
            f"Tool call arguments must be a JSON object, got {type(args).__name__}. "
            f"Raw: {raw_args[:200]}"
        )

    ids_raw = args.get("selected_ids")
    if ids_raw is None:
        raise ValueError(
            f"Tool call arguments missing 'selected_ids' key. Raw: {raw_args[:200]}"
        )
    if not isinstance(ids_raw, list):
        raise ValueError(
            f"'selected_ids' must be a JSON array, got {type(ids_raw).__name__}"
        )

    selected: list[int] = []
    for i, item in enumerate(ids_raw):
        if not isinstance(item, int):
            raise ValueError(
                f"Selected ID at index {i} must be an integer, "
                f"got {type(item).__name__}: {item!r}"
            )
        if item not in candidate_ids:
            raise ValueError(
                f"Selected ID {item} (index {i}) is not in the candidate list "
                f"sent to the model."
            )
        selected.append(item)

    return selected
