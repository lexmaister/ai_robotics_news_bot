import os

from openai import OpenAI
from prefect import flow, task, get_run_logger


@task(name="openrouter_chat", retries=2, retry_delay_seconds=5)
def openrouter_chat(prompt: str, model: str) -> str:
    logger = get_run_logger()

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Put it into private/env/prefect.dev.env "
            "and inject it via docker-compose env_file (or export locally)."
        )

    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            # Optional but recommended by OpenRouter for attribution/analytics
            "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "http://localhost"),
            "X-Title": os.getenv("OPENROUTER_APP_TITLE", "AI & Robotics News Bot"),
        },
    )

    logger.info("Calling OpenRouter model=%s", model)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You help test connectivity for an AI/Robotics news curation system.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    content = resp.choices[0].message.content
    if not content:
        raise RuntimeError("OpenRouter returned empty message content")

    return content.strip()


@flow(name="test_openrouter")
def test_openrouter_flow() -> str:
    logger = get_run_logger()

    model = os.getenv("OPENROUTER_MODEL", "stepfun/step-3.5-flash:free")
    prompt = "Reply with exactly one sentence confirming you can read this message."

    result = openrouter_chat(prompt=prompt, model=model)
    logger.info("OpenRouter response: %s", result)
    return result


if __name__ == "__main__":
    test_openrouter_flow()
