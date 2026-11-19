"""Adaptive Card for the Teams Workflows webhook ("Post to a channel when a webhook request is received")."""
from __future__ import annotations

from datetime import date

from .ai import Triage
from .config import Product
from .digest import Digest, Issue, Period
from .play_vitals import ANR_THRESHOLD_PCT, CRASH_THRESHOLD_PCT


def _delta(cur: int, prev: int) -> str:
    if prev == 0:
        return "" if cur == 0 else " (new)"
    pct = round(100 * (cur - prev) / prev)
    if pct == 0:
        return " (=)"
    return f" ({'▲' if pct > 0 else '▼'}{abs(pct)}%)"


def _crash_free(cur: Period, prev: Period) -> str:
    c, p = cur.crash_free_pct, prev.crash_free_pct
    if c is None:
        return "n/a"
    if p is None:
        return f"{c:.2f}%"
    diff = round(c - p, 2)
    arrow = "=" if diff == 0 else ("▲" if diff > 0 else "▼")
    return f"{c:.2f}% ({arrow}{abs(diff):.2f} pts)"


def _text(text: str, **kw) -> dict:
    return {"type": "TextBlock", "text": text, "wrap": True, **kw}


def _issue_block(issue: Issue, product: Product, tag: str | None = None) -> dict:
    head = f"**{issue.title}** · {issue.kind}"
    if tag:
        head = f"{tag} {head}"
    detail = [f"{issue.users} users · {issue.events} events"]
    if issue.top_versions:
        detail.append("v" + ", v".join(v for v, _ in issue.top_versions))
    if issue.top_devices:
        detail.append(", ".join(v for v, _ in issue.top_devices[:2]))
    if issue.top_os:
        detail.append("Android " + ", ".join(v for v, _ in issue.top_os[:2]))
    items = [_text(head, spacing="Small")]
    if issue.location:
        items.append(_text(f"`{issue.location}`", isSubtle=True, spacing="None", fontType="Monospace"))
    items.append(_text(" · ".join(detail) + f" · [Open]({product.issue_url(issue.issue_id)})",
                       isSubtle=True, spacing="None", size="Small"))
    return {"type": "Container", "items": items, "spacing": "Small"}


def _section(title: str, issues: list[Issue], product: Product, tag: str | None = None) -> list[dict]:
    if not issues:
        return []
    return [_text(title, weight="Bolder", spacing="Medium", separator=True)] + [
        _issue_block(i, product, tag) for i in issues
    ]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _breakdown_section(title: str, facts: list[dict]) -> list[dict]:
    if not facts:
        return []
    return [_text(title, weight="Bolder", spacing="Medium", separator=True),
            {"type": "FactSet", "facts": facts, "spacing": "Small"}]


def _rate_value(block: dict, threshold: float) -> str:
    rate, prev, avg = block.get("rate"), block.get("prev_rate"), block.get("rate_28d")
    text = "n/a" if rate is None else f"{rate:.2f}%"
    if rate is not None and prev is not None:
        diff = round(rate - prev, 2)
        arrow = "=" if diff == 0 else ("▲" if diff > 0 else "▼")
        text += f" ({arrow}{abs(diff):.2f} pts)"
    if avg is not None:
        text += f" · 28-day {avg:.2f}%"
        if avg >= threshold:
            text += f" ⚠️ over Play's {threshold}% limit"
    return text


def _play_section(play: dict | None, top_n: int) -> list[dict]:
    """Google Play vitals: its own day (Play's daily data lags) and app-wide numbers."""
    if play is None:
        return []
    if play.get("error"):
        return [_text("▶️ Google Play vitals", weight="Bolder", spacing="Medium", separator=True),
                _text(f"Play Console data unavailable today ({play['error']}). The Crashlytics numbers "
                      "above are complete.", isSubtle=True, spacing="Small")]

    day = date.fromisoformat(play["day"])
    out = [
        _text(f"▶️ Google Play vitals · {day:%d %b}", weight="Bolder", spacing="Medium", separator=True),
        _text("All app versions · Play's latest full day (US Pacific), so it trails the window above · "
              "user-perceived rates, the ones Play judges the app on",
              isSubtle=True, spacing="None", size="Small"),
    ]
    crash, anr = play.get("crash"), play.get("anr")
    if not crash and not anr:
        out.append(_text("Play has no vitals for this day (too few users, or not published yet).",
                         isSubtle=True, spacing="Small"))
        return out

    facts = []
    if crash:
        facts.append({"title": "Crash rate", "value": _rate_value(crash, CRASH_THRESHOLD_PCT)})
    if anr:
        facts.append({"title": "ANR rate", "value": _rate_value(anr, ANR_THRESHOLD_PCT)})
    users = (crash or {}).get("users") or (anr or {}).get("users")
    if users:
        facts.append({"title": "Users (Play)", "value": f"{users:,}"})
    out.append({"type": "FactSet", "facts": facts, "spacing": "Small"})

    issues = (play.get("issues") or [])[:top_n]
    if issues:
        out.append(_text(f"Top {len(issues)} Play issues by users", weight="Bolder", spacing="Small"))
    for i in issues:
        kind = "ANR" if i.get("type") == "ANR" else "Crash"
        title = (i.get("cause") or kind).split(".")[-1] or kind
        # No class name (usual for ANRs): the title already is the kind, so don't repeat it.
        head = f"**{title}**" if title == kind else f"**{title}** · {kind}"
        items = [_text(head, spacing="Small")]
        if i.get("location"):
            items.append(_text(f"`{i['location']}`", isSubtle=True, spacing="None", fontType="Monospace"))
        detail = [f"{_plural(i.get('users') or 0, 'user')} · {_plural(i.get('reports') or 0, 'report')}"]
        if i.get("last_version"):
            detail.append(f"last seen in build {i['last_version']}")
        line = " · ".join(detail)
        if i.get("url"):
            line += f" · [Open]({i['url']})"
        items.append(_text(line, isSubtle=True, spacing="None", size="Small"))
        out.append({"type": "Container", "items": items, "spacing": "Small"})
    return out


def build_card(
    digest: Digest,
    *,
    product: Product,
    window_label: str,
    top_n: int,
    max_flagged: int,
    triage: Triage | None,
    ai_enabled: bool,
) -> dict:
    cur, prev = digest.current, digest.previous
    body: list[dict] = [
        _text(f"📊 {product.display_name} — daily crash review", weight="Bolder", size="Large"),
        _text(window_label, isSubtle=True, spacing="None", size="Small"),
    ]
    if digest.versions:
        n = len(digest.versions)
        scope = "latest version" if n == 1 else f"latest {n} versions"
        label = "App version" if n == 1 else "App versions"
        applies = "Crashlytics numbers" if digest.play is not None else "all numbers below"
        body.append(_text(f"{label}: {', '.join(digest.versions)} ({scope}, {applies})",
                          isSubtle=True, spacing="None", size="Small"))
    play = _play_section(digest.play, top_n)

    if digest.looks_like_no_data:
        body.append(_text(
            "⚠️ **No data received for this window.** Crashlytics returned no crashes and no "
            "active users, which usually means crash reporting isn't arriving, not that the day "
            "was clean. Check the Crashlytics dashboard.", color="Warning"))
        return _wrap(body + play)

    facts = [
        {"title": "Crashes", "value": f"{cur.crashes}{_delta(cur.crashes, prev.crashes)}"},
        {"title": "ANRs", "value": f"{cur.anrs}{_delta(cur.anrs, prev.anrs)}"},
        {"title": "Users affected", "value": f"{cur.users_affected}{_delta(cur.users_affected, prev.users_affected)}"},
    ]
    if digest.sessions_enabled:
        facts.append({"title": "Crash-free users", "value": _crash_free(cur, prev)})
    body.append({"type": "FactSet", "facts": facts, "spacing": "Medium"})

    if not digest.has_any_events:
        body.append(_text("✅ No crashes or ANRs in this window.", spacing="Medium"))
        return _wrap(body + play)

    # AI section
    by_id = {i.issue_id: i for i in digest.issues}
    if triage:
        ai_items = [_text("🤖 Look at first", weight="Bolder", spacing="Medium", separator=True),
                    _text(triage.summary, spacing="Small")]
        for n, p in enumerate(triage.priorities, 1):
            issue = by_id[p["issue_id"]]
            ai_items.append(_text(
                f"{n}. **{issue.title}** ({issue.kind}) — {p['reason']} "
                f"[Open]({product.issue_url(issue.issue_id)})", spacing="Small"))
        body.extend(ai_items)
    elif ai_enabled:
        body.append(_text("🤖 AI summary unavailable today — numbers below are complete.",
                          isSubtle=True, spacing="Medium", separator=True))

    body += _breakdown_section(
        "🧩 By crash type",
        [{"title": t["type"] if t["type"] == t["kind"] else f"{t['type']} ({t['kind']})",
          "value": f"{_plural(t['events'], 'event')} · {_plural(t['issues'], 'issue')}"}
         for t in digest.by_crash_type(top_n)])
    body += _breakdown_section(
        "📱 By device model",
        [{"title": d["device"], "value": f"{_plural(d['events'], 'event')} · {_plural(d['users'], 'user')}"}
         for d in digest.devices[:top_n]])

    body += _section("🆕 New issues", digest.new_issues[:max_flagged], product)
    body += _section("🔁 Regressed (came back after being closed)", digest.returning_issues[:max_flagged], product)
    body += _section(f"🔥 Top {top_n} by users affected", digest.top_issues(top_n), product)

    body += play
    footer = "Numbers from Firebase Crashlytics"
    if digest.play is not None and not digest.play.get("error"):
        footer += " and Google Play Console"
    if triage:
        footer += " · summary by Claude, verify before acting"
    body.append(_text(footer, isSubtle=True, size="Small", spacing="Medium", separator=True))
    return _wrap(body)


def build_error_card(product: Product, window_label: str, message: str) -> dict:
    return _wrap([
        _text(f"📊 {product.display_name} — daily crash review", weight="Bolder", size="Large"),
        _text(window_label, isSubtle=True, spacing="None", size="Small"),
        _text(f"❌ **Digest failed:** {message}", color="Attention", spacing="Medium"),
        _text("The job owner has been alerted. Check Crashlytics directly today.", isSubtle=True),
    ])


def _wrap(body: list[dict]) -> dict:
    """Message envelope accepted by the Teams Workflows webhook template."""
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "msteams": {"width": "Full"},
                "body": body,
            },
        }],
    }
