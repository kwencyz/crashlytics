"""Post to a Teams Workflows webhook with retries.

The webhook URL is a secret (anyone holding it can post to the channel), so it is never
logged: error text is passed through _redact(), which strips URL paths and query strings.
"""
from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

_PATH_OR_QUERY = re.compile(r"/[^\s'\"),]*")


class TeamsPostError(RuntimeError):
    pass


def _redact(text: str, webhook_url: str) -> str:
    text = text.replace(webhook_url, "<webhook>")
    parts = urlsplit(webhook_url)
    for piece in (parts.path, parts.query):
        if piece:
            text = text.replace(piece, "<redacted>")
    return _PATH_OR_QUERY.sub("/<redacted>", text)


def post_card(webhook_url: str, message: dict, *, attempts: int = 3, backoff_s: float = 5.0) -> None:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(webhook_url, json=message, timeout=30)
            if 200 <= resp.status_code < 300:   # Workflows normally answers 202 Accepted
                log.info("Posted to Teams (HTTP %s)", resp.status_code)
                return
            last = f"HTTP {resp.status_code}: {_redact(resp.text[:300], webhook_url)}"
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {_redact(str(exc), webhook_url)[:300]}"
        log.warning("Teams post attempt %d/%d failed: %s", attempt, attempts, last)
        if attempt < attempts:
            time.sleep(backoff_s * attempt)
    raise TeamsPostError(f"Teams post failed after {attempts} attempts: {last}")
