"""Claude writes the triage note. It returns structured output via a forced tool call,
and anything it says is checked against the payload before it reaches the card."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are the on-call mobile engineer writing the morning crash review for an Android payment app's team.
You receive aggregated Crashlytics data for the last 24 hours compared with the 24 hours before.

Write for busy engineers:
- summary: 1-2 sentences on how the day looked overall (better/worse/steady, and the main reason).
- priorities: up to 3 issues to look at first, most urgent first. For each, one short reason that
  explains WHY it matters (new in a release, concentrated on one device or Android version,
  growing, many users, ANR on a main flow, etc.) and, if the frames suggest it, where to start looking.
- Prefer new and returning issues, issues concentrated in the latest app version, and high user counts.
- by_crash_type and by_device summarize all issues in scope; use them to spot one exception class or
  one device model driving most of the day.

Rules:
- Only refer to issues by the issue_id values given in the data. Never invent issues.
- Do not restate exact counts or percentages; the card shows the numbers next to your text.
- Do not guess at business impact you can't see in the data.
- If the day is quiet, say so plainly and return an empty priorities list.
"""

TOOL = {
    "name": "submit_digest",
    "description": "Submit the crash triage note for today's digest.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "1-2 sentences, plain text."},
            "priorities": {
                "type": "array",
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "issue_id": {"type": "string"},
                        "reason": {"type": "string", "description": "One sentence, plain text."},
                    },
                    "required": ["issue_id", "reason"],
                },
            },
        },
        "required": ["summary", "priorities"],
    },
}


@dataclass
class Triage:
    summary: str
    priorities: list[dict] = field(default_factory=list)   # [{issue_id, reason}]
    dropped: list[str] = field(default_factory=list)       # issue_ids the model made up


def validate(raw: dict, payload: dict) -> Triage | None:
    known = {i["issue_id"] for i in payload.get("issues", [])}
    summary = (raw.get("summary") or "").strip()
    priorities, dropped = [], []
    for p in (raw.get("priorities") or [])[:3]:
        if not isinstance(p, dict):
            dropped.append(str(p))
            continue
        iid = str(p.get("issue_id", "")).strip()
        reason = (p.get("reason") or "").strip()
        if iid in known and reason:
            priorities.append({"issue_id": iid, "reason": reason[:300]})
        else:
            dropped.append(iid)
    if dropped:
        log.warning("AI referenced unknown issue ids, dropped: %s", dropped)
    if not summary:
        return None
    return Triage(summary=summary[:500], priorities=priorities, dropped=dropped)


def load_triage(path: str, payload: dict) -> Triage | None:
    """Triage written by a Claude routine instead of an API call. Same validation as the API path."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        log.exception("could not read triage file %s; posting without AI section", path)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("summary", ""), str):
        log.warning("triage file %s has the wrong shape; posting without AI section", path)
        return None
    return validate(raw, payload)


def write_triage(payload: dict, *, model: str, max_tokens: int) -> Triage | None:
    """Returns None on any failure; the caller posts the card without the AI section."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set; skipping AI section")
        return None
    try:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=[TOOL],
            tool_choice={"type": "tool", "name": TOOL["name"]},
            messages=[{"role": "user", "content": "Crash data:\n" + json.dumps(payload, indent=1, default=str)}],
        )
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == TOOL["name"]:
                return validate(block.input, payload)
        log.warning("AI returned no tool call")
        return None
    except Exception:  # noqa: BLE001 - any AI failure must not block the digest
        log.exception("AI call failed; posting digest without AI section")
        return None
