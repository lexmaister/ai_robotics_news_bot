"""
flows/start_flows.py

Bootstrap Prefect deployments for this repo.

This script DOES NOT start Prefect server/worker. Those are managed by docker-compose:
  docker compose --profile server up -d
  docker compose --profile worker up -d

Instead, this script:
- validates config using src/config.py
- registers (deploys) flows using the Prefect CLI

Usage:
```
docker exec -it news_worker python -m flows.start_flows
```
"""

from __future__ import annotations

import subprocess
import sys
import yaml
from pathlib import Path

from src.config import EnvSettings, load_settings, load_whitelist


def _interval_seconds_from_settings(settings_path: Path) -> int:
    raw = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
    runs_per_day = int((raw.get("orchestration") or {}).get("runs_per_day", 0))
    if runs_per_day <= 0:
        raise ValueError(f"orchestration.runs_per_day missing or invalid in {settings_path}")
    if 86400 % runs_per_day != 0:
        # Not fatal, but makes it explicit you may get fractional hours if you ever display it.
        # Prefect interval schedule works in seconds, so integer division is fine.
        pass
    return int(24 * 3600 / runs_per_day)


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}")


def main() -> None:
    env = EnvSettings()

    # Validate config early (will raise if queries too long etc.)
    settings = load_settings(env)
    whitelist = load_whitelist(env)

    print("Config OK:")
    print(f"- settings_path: {env.settings_path}")
    print(f"- sources_whitelist_path: {env.sources_whitelist_path}")
    print(f"- last_news_path: {env.last_news_path}")
    print(f"- queries: {len(settings.queries)}")
    print(f"- whitelist groups: {len(whitelist)}")

    # Create/update deployment for the production flow
    # Target pool must match your worker: default-agent-pool
    interval_seconds = _interval_seconds_from_settings(env.settings_path)
    print(f"Deploying with interval_seconds={interval_seconds} (from {env.settings_path})")
    run([
        "prefect",
        "deploy",
        "flows/daily_news_flow.py:daily_news_flow",
        "--name",
        "prod",
        "--pool",
        "default-agent-pool",
        "--interval",
        str(interval_seconds),
    ])

    print("Deployment applied: daily-news-flow/prod")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise