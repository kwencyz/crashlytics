"""Build the ONLY data Claude ever sees. Allow-list: every field is copied explicitly.

Never included: user id/name/email, installation ids, custom keys, logs, breadcrumbs,
exception messages/subtitles. The API reader (crashlytics_api.py) doesn't copy those either;
this is the second layer.
"""
from __future__ import annotations

import re

from .digest import Digest, Issue

MAX_STR = 200

# Belt-and-braces scrubbing on the few free-text fields we do send (symbols, file names).
_REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "[uuid]"),
    (re.compile(r"\b\d[\d -]{10,}\d\b"), "[number]"),      # long digit runs (card-like, phone-like)
]


def scrub(text: str | None) -> str | None:
    if text is None:
        return None
    text = str(text)[:MAX_STR]
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def _issue_payload(issue: Issue) -> dict:
    return {
        "issue_id": issue.issue_id,
        "kind": issue.kind,
        "exception_type": scrub(issue.exception_type),
        "location": scrub(issue.location),
        "users_affected": issue.users,
        "events": issue.events,
        "is_new": issue.is_new,
        "is_returning": issue.is_returning,
        "app_versions": [[scrub(v), n] for v, n in issue.top_versions],
        "devices": [[scrub(v), n] for v, n in issue.top_devices],
        "android_versions": [[scrub(v), n] for v, n in issue.top_os],
        "top_frames": [
            {"symbol": scrub(f.get("symbol")), "file": scrub(f.get("file")), "line": f.get("line")}
            for f in issue.frames[:5]
        ],
    }


def build_ai_payload(digest: Digest, *, product_name: str, window_label: str, top_n: int, max_flagged: int) -> dict:
    # Send the union of top issues + flagged issues, de-duplicated, capped.
    chosen: dict[str, Issue] = {}
    for issue in digest.top_issues(top_n) + digest.new_issues[:max_flagged] + digest.returning_issues[:max_flagged]:
        chosen.setdefault(issue.issue_id, issue)

    return {
        "product": product_name,
        "window": window_label,
        # Numbers below only count these app versions (empty = all versions).
        "app_versions_in_scope": list(digest.versions),
        "totals": {
            "current": {
                "crashes": digest.current.crashes,
                "anrs": digest.current.anrs,
                "users_affected": digest.current.users_affected,
                "crash_free_users_pct": digest.current.crash_free_pct,
            },
            "previous": {
                "crashes": digest.previous.crashes,
                "anrs": digest.previous.anrs,
                "users_affected": digest.previous.users_affected,
                "crash_free_users_pct": digest.previous.crash_free_pct,
            },
        },
        "issues": [_issue_payload(i) for i in chosen.values()],
    }
