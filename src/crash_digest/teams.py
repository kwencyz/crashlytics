"""Post to a Teams Workflows webhook with retries."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)


class TeamsPostError(RuntimeError):
    pass


def post_card(webhook_url: str, message: dict, *, attempts: int = 3, backoff_s: float = 5.0) -> None:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(webhook_url, json=message, timeout=30)
            if 200 <= resp.status_code < 300:   # Workflows normally answers 202 Accepted
                log.info("Posted to Teams (HTTP %s)", resp.status_code)
                return
            last = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as exc:
            last = repr(exc)
        log.warning("Teams post attempt %d/%d failed: %s", attempt, attempts, last)
        if attempt < attempts:
            time.sleep(backoff_s * attempt)
    raise TeamsPostError(f"Teams post failed after {attempts} attempts: {last}")
