"""Adaptive Card for the Teams Workflows webhook ("Post to a channel when a webhook request is received")."""
from __future__ import annotations

from .ai import Triage
from .config import Product
from .digest import Digest, Issue, Period


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
        body.append(_text(f"{label}: {', '.join(digest.versions)} ({scope}, all numbers below)",
                          isSubtle=True, spacing="None", size="Small"))

    if digest.looks_like_no_data:
        body.append(_text(
            "⚠️ **No data received for this window.** Crashlytics returned no crashes and no "
            "active users, which usually means crash reporting isn't arriving, not that the day "
            "was clean. Check the Crashlytics dashboard.", color="Warning"))
        return _wrap(body)

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
        return _wrap(body)

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

    footer = "Numbers from Firebase Crashlytics"
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
