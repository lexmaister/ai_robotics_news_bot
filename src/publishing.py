"""
src/publishing.py

Telegram channel publishing for the AI/Robotics news bot.

Task 6 responsibilities:
- Receive the list of curated articles selected by Task 5.
- Format each article as an HTML Telegram message.
- POST messages to the Telegram Bot API via httpx (synchronous — no event-loop
  conflicts inside a Prefect worker thread).
- Raise TelegramPublishError on any API-level or network-level error so the
  Prefect task can decide whether to retry or fail the flow.

Design notes:
- Synchronous httpx.post avoids the asyncio complexity of python-telegram-bot v20+
  inside a Prefect sync-task context.
- Format: "{emoji} <b>Category</b>\\n\\nTitle\\n\\nURL"
  The bare URL at the end triggers Telegram's automatic link-preview (article
  thumbnail + excerpt), which is the best UX for a news channel.
- HTML parse_mode: category and title are html.escape()-ed to prevent injection.
- Fail-fast on any error: the caller (task6) is responsible for deciding whether
  to retry or abort, and for committing DB state for already-published articles.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_BASE = "https://api.telegram.org"

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
    Format a single article as a Telegram HTML message.

    Example output (raw text before Telegram renders HTML):
        🔬 <b>GenAI Research</b>

        GPT-5 Achieves Human-Level Reasoning in Latest Benchmark

        https://example.com/article

    The bare URL at the end triggers Telegram's link-preview (shows the article's
    thumbnail image and excerpt). The title is HTML-escaped to prevent tags in
    scraped titles from breaking the parse.
    """
    emoji = CATEGORY_EMOJI.get(article.category, _DEFAULT_EMOJI)
    safe_category = html.escape(article.category)
    safe_title = html.escape(article.title)
    return f"{emoji} <b>{safe_category}</b>\n\n{safe_title}\n\n{article.link}"


# ---------------------------------------------------------------------------
# Telegram Bot API call
# ---------------------------------------------------------------------------


def send_telegram_message(
    *,
    bot_token: str,
    channel_id: str,
    text: str,
    timeout: float = 30.0,
) -> None:
    """
    POST a single message to a Telegram channel via the Bot HTTP API.

    Args:
        bot_token:  The bot token from @BotFather (without "bot" prefix).
        channel_id: Channel username ("@mychannel") or numeric id ("-1001234567890").
        text:       HTML-formatted message text.
        timeout:    Request timeout in seconds.

    Raises:
        TelegramPublishError: on any HTTP error or Telegram API-level error.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    payload: dict = {
        "chat_id": channel_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        response = httpx.post(url, json=payload, timeout=timeout)
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
