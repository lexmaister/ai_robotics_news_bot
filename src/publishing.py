"""
src/publishing.py

Telegram publication helpers for Task 6.
Formats articles as HTML messages and POSTs them via the Bot HTTP API (synchronous httpx).
Raises TelegramPublishError on any API or network failure.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"
_CHANNEL_URL = "https://t.me/robotics_ai_news"

# ---------------------------------------------------------------------------
# Category → emoji mapping
# Keep in sync with PREFERRED_CATEGORIES in src/curation.py
# ---------------------------------------------------------------------------

CATEGORY_EMOJI: dict[str, str] = {
    "Agentic AI": "🤖",
    "Humanoid Robots": "🦾",
    "AI Security": "🔒",
    "Robotics Market": "📊",
    "AI Jobs": "💼",
    "AI Policy": "⚖️",
    "Health AI": "🏥",
    "GenAI Research": "🔬",
    "GenAI Media": "🎨",
    "AI Drones": "🚁",
    "Enterprise AI": "🏢",
    "Open Source": "💻",
    "Hardware": "⚙️",
    "Science": "🧪",
    "Markets": "💹",
}

_DEFAULT_EMOJI = "📡"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ArticleToPublish:
    """Single article ready to be posted to the Telegram channel."""

    id: int
    title: str
    category: str
    link: str


class TelegramPublishError(RuntimeError):
    """Raised when the Telegram Bot API returns an error or the HTTP call fails."""


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def format_article_message(article: ArticleToPublish) -> str:
    """
    Format an article as a Telegram HTML message.

    Pattern:
        {emoji} <b>Category</b>

        Title

        <a href="URL">Read full article</a>

        <a href="channel">@robotics_ai_news</a>
    """
    emoji = CATEGORY_EMOJI.get(article.category, _DEFAULT_EMOJI)
    safe_category = html.escape(article.category)
    safe_title = html.escape(article.title)
    safe_link = html.escape(article.link.strip())
    return (
        f"{emoji} <b>{safe_category}</b>\n\n"
        f"{safe_title}\n\n"
        f'<a href="{safe_link}">Read full article</a>\n\n'
        f'<a href="{_CHANNEL_URL}">@robotics_ai_news</a>'
    )


# ---------------------------------------------------------------------------
# Telegram Bot API call
# ---------------------------------------------------------------------------


def send_telegram_message(
    *,
    bot_token: str,
    channel_id: str,
    text: str,
    timeout: float = 30.0,
    proxy_url: str | None = None,
) -> None:
    """
    POST a single message to a Telegram channel via the Bot HTTP API.

    Args:
        bot_token:  The bot token from @BotFather (without "bot" prefix).
        channel_id: Channel username ("@mychannel") or numeric id ("-1001234567890").
        text:       HTML-formatted message text.
        timeout:    Request timeout in seconds.
        proxy_url:  Optional proxy URL to use.

    Raises:
        TelegramPublishError: on any HTTP error or Telegram API-level error.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload: dict = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
    }

    client_kwargs: dict = {}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        with httpx.Client(**client_kwargs) as client:
            response = client.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise TelegramPublishError(
            f"HTTP {exc.response.status_code} from Telegram API: {exc.response.text[:400]}"
        ) from exc
    except httpx.RequestError as exc:
        raise TelegramPublishError(
            f"Network error calling Telegram API: {exc}"
        ) from exc

    result = response.json()
    if not result.get("ok"):
        error_code = result.get("error_code", "?")
        description = result.get("description", "Unknown Telegram API error")
        raise TelegramPublishError(f"Telegram API error {error_code}: {description}")

    logger.debug(
        "Message sent OK: message_id=%s chat_id=%s",
        result.get("result", {}).get("message_id"),
        channel_id,
    )


# ---------------------------------------------------------------------------
# Weekly report formatting
# ---------------------------------------------------------------------------


def format_weekly_report_message(report_text: str, *, max_chars: int = 4096) -> str:
    """
    Format the LLM-generated weekly report text for Telegram HTML mode.

    Rules:
    - The first non-empty line is wrapped in <b>…</b> to serve as the heading.
    - All lines are HTML-escaped to prevent injection of raw tags.
    - A channel footer link is appended.
    - The total message is trimmed to max_chars if needed, cutting at the last
      newline boundary to avoid mid-line truncation.

    Args:
        report_text: Plain-text report from the LLM (may contain newlines).
        max_chars:   Maximum total character count of the returned HTML string.
                     Telegram hard-limits messages to 4096 characters.

    Returns:
        HTML-formatted string ready to pass to send_telegram_message().
    """
    lines = report_text.strip().splitlines()
    if not lines:
        return html.escape("[No report content]")

    formatted_lines: list[str] = []
    for i, line in enumerate(lines):
        escaped = html.escape(line)
        if i == 0 and escaped.strip():
            # Bold the heading line
            formatted_lines.append(f"<b>{escaped}</b>")
        else:
            formatted_lines.append(escaped)

    body = "\n".join(formatted_lines)
    footer = f'\n\n<a href="{_CHANNEL_URL}">@robotics_ai_news</a>\n\n#report'
    message = body + footer

    if len(message) > max_chars:
        # Preserve footer; trim body at last newline boundary
        trim_to = max_chars - len(footer) - 3  # reserve room for "..."
        trimmed_body = message[:trim_to]
        last_nl = trimmed_body.rfind("\n")
        if last_nl > 0:
            trimmed_body = trimmed_body[:last_nl].rstrip()
        message = trimmed_body + "..." + footer

    return message
