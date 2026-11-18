"""Read crash data from the Firebase Crashlytics API (v1alpha).

This is the same API the Firebase CLI and the Firebase MCP server use. It is not a
documented public API, so Google may change it; if it breaks, the job fails loudly
(error card + non-zero exit) instead of posting wrong numbers.

Privacy: every value we keep is copied by name. Exception messages, issue subtitles,
installation IDs, breadcrumbs, logs, memory/storage and custom keys are never read
out of a response, so they can't reach the payload, the card or Claude.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

from .config import Product
from .windows import Windows

log = logging.getLogger(__name__)

API_ROOT = "https://firebasecrashlytics.googleapis.com/v1alpha"
RETRYABLE = {429, 500, 502, 503, 504}

SIGNAL_NEW = "SIGNAL_FRESH"
SIGNAL_RETURNING = "SIGNAL_REGRESSED"
VERSION_LOOKBACK = timedelta(days=30)   # where we look for "the newest N versions"
SAMPLE_LIMIT = 50                       # issues that get a sample event (exception class for "By crash type")


class CrashlyticsAPIError(RuntimeError):
    pass


@dataclass
class QueryResults:
    """Rows in the shape build_digest() expects."""
    headline: list[dict] = field(default_factory=list)
    crash_free: list[dict] | None = None
    issues: list[dict] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)   # app versions the numbers are limited to; [] = all
    devices: list[dict] = field(default_factory=list)    # [{device, events, users}] app-wide, crashes + ANRs


class CrashlyticsClient:
    def __init__(self, product: Product, *, session=None, credentials=None,
                 attempts: int = 3, backoff_s: float = 2.0):
        self._base = f"{API_ROOT}/projects/{product.project_number}/apps/{product.app_id}"
        self._quota_project = str(product.project_number)
        self._session = session or requests.Session()
        self._creds = credentials
        self._attempts = attempts
        self._backoff_s = backoff_s
        # Version displayNames ("3.5.14 (2026111301)") every report/event call is limited to. [] = all.
        self.version_names: list[str] = []

    def _token(self) -> str:
        if self._creds is None:
            from .gauth import google_credentials  # lazy: tests and fixture runs don't need it

            self._creds = google_credentials()
        if not self._creds.valid:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return self._creds.token

    def get(self, path: str, params: list[tuple[str, str]]) -> dict:
        last = None
        for attempt in range(1, self._attempts + 1):
            try:
                resp = self._session.get(
                    self._base + path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token()}",
                        # Without this, user credentials bill the call to gcloud's own project,
                        # where the Crashlytics API is disabled.
                        "x-goog-user-project": self._quota_project,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                last = repr(exc)
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRYABLE:
                    break
            log.warning("Crashlytics GET %s attempt %d/%d failed: %s", path, attempt, self._attempts, last)
            if attempt < self._attempts:
                time.sleep(self._backoff_s * attempt)
        raise CrashlyticsAPIError(f"GET {path} failed: {last}")

    def report(self, name: str, start: datetime, end: datetime, *, page_size: int = 10,
               error_types: tuple[str, ...] = (), issue_id: str | None = None,
               all_versions: bool = False) -> list[dict]:
        params = _filter(start, end, error_types=error_types, issue_id=issue_id,
                         versions=() if all_versions else self.version_names)
        params.append(("page_size", str(page_size)))
        return self.get(f"/reports/{name}", params).get("groups") or []

    def latest_event(self, issue_id: str, start: datetime, end: datetime) -> dict | None:
        params = _filter(start, end, issue_id=issue_id, versions=self.version_names)
        params.append(("page_size", "1"))
        events = self.get("/events", params).get("events") or []
        return events[0] if events else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter(start: datetime, end: datetime, *, error_types=(), issue_id=None, versions=()) -> list[tuple[str, str]]:
    params = [("filter.interval.start_time", _iso(start)), ("filter.interval.end_time", _iso(end))]
    params += [("filter.issue.error_types", t) for t in error_types]
    params += [("filter.version.display_names", v) for v in versions]
    if issue_id:
        params.append(("filter.issue.id", issue_id))
    return params


def _semver(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+ ]", version))


def latest_versions(api: CrashlyticsClient, end: datetime, n: int) -> tuple[list[str], list[str]]:
    """Newest n app versions seen in the last 30 days.

    Returns (display versions like "3.5.14", every matching displayName like "3.5.14 (2026111301)").
    The API filter only accepts displayNames, and one version can have several builds.
    """
    builds: dict[str, list[str]] = {}
    for g in api.report("topVersions", end - VERSION_LOOKBACK, end, page_size=100, all_versions=True):
        v = g.get("version") or {}
        if v.get("displayVersion") and v.get("displayName"):
            builds.setdefault(v["displayVersion"], []).append(v["displayName"])
    newest = sorted(builds, key=_semver, reverse=True)[:n]
    return newest, [name for dv in newest for name in builds[dv]]


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metrics(group: dict) -> dict:
    metrics = group.get("metrics") or [{}]
    return metrics[0] or {}


def _summary(groups: list[dict]) -> dict:
    """appSummary returns a single group (or none on an empty window)."""
    return _metrics(groups[0]) if groups else {}


def _breakdown(groups: list[dict], key: str, label_field: str) -> list[dict]:
    """Per-issue breakdowns can include rows with 0 events (e.g. other app versions); drop those."""
    out = []
    for g in groups:
        label = (g.get(key) or {}).get(label_field)
        count = _int(_metrics(g).get("eventsCount")) or 0
        if count > 0:
            out.append({"value": label, "count": count})
    return out


def _frames(raw: list[dict] | None) -> list[dict]:
    return [
        {"symbol": f.get("symbol"), "file": f.get("file"), "line": _int(f.get("line"))}
        for f in (raw or [])[:5]
    ]


def _sample_fields(event: dict | None) -> dict:
    """Exception class, top 5 frames and blame location from one event. Nothing else."""
    empty = {"exception_type": None, "frames": [], "blame_symbol": None, "blame_file": None, "blame_line": None}
    if not event:
        return empty
    blame = event.get("blameFrame") or {}
    blame_symbol = blame.get("symbol")

    exception_type, frames = None, None
    exceptions = event.get("exceptions") or []
    if exceptions:
        # Java "Caused by" chains are outer → root; the root cause is what the issue is named after.
        exception_type = exceptions[-1].get("type")
        holder = next(
            (e for e in reversed(exceptions)
             if blame_symbol and any(f.get("symbol") == blame_symbol for f in e.get("frames") or [])),
            exceptions[-1],
        )
        frames = holder.get("frames")
    if not frames:  # ANRs (and native crashes) only have threads
        threads = event.get("threads") or []
        thread = next((t for t in threads if t.get("blamed")), None) or next(
            (t for t in threads if t.get("crashed")), None)
        frames = (thread or {}).get("frames")

    return {
        "exception_type": exception_type,
        "frames": _frames(frames),
        "blame_symbol": blame_symbol,
        "blame_file": blame.get("file"),
        "blame_line": _int(blame.get("line")),
    }


def _issue_row(group: dict) -> dict:
    issue = group.get("issue") or {}
    m = _metrics(group)
    signals = {s.get("signal") for s in issue.get("signals") or []}
    return {
        "issue_id": issue.get("id"),
        "error_type": issue.get("errorType"),
        "events": _int(m.get("eventsCount")) or 0,
        "users": _int(m.get("impactedUsersCount")) or 0,
        "is_new": SIGNAL_NEW in signals,
        "is_returning": SIGNAL_RETURNING in signals,
        "top_versions": [],
        "top_devices": [],
        "top_os": [],
        **_sample_fields(None),
    }


def _needs_detail(rows: list[dict], top_n: int, max_flagged: int) -> list[dict]:
    """Only issues that end up on the card (or in the AI payload) get the extra calls."""
    by_users = sorted(rows, key=lambda r: (r["users"], r["events"]), reverse=True)
    chosen: dict[str, dict] = {}
    for r in (by_users[:top_n]
              + [r for r in by_users if r["is_new"]][:max_flagged]
              + [r for r in by_users if r["is_returning"]][:max_flagged]):
        chosen.setdefault(r["issue_id"], r)
    return list(chosen.values())


def run_queries(product: Product, windows: Windows, *, top_n: int = 5, max_flagged: int = 5,
                client: CrashlyticsClient | None = None) -> QueryResults:
    api = client or CrashlyticsClient(product)
    periods = {
        "current": (windows.window_start, windows.window_end),
        "previous": (windows.prev_window_start, windows.window_start),
    }

    results = QueryResults(crash_free=[])
    if product.latest_versions > 0:
        results.versions, api.version_names = latest_versions(api, windows.window_end, product.latest_versions)
        if not api.version_names:
            log.warning("[%s] no app versions found in the last 30 days; counting all versions", product.key)
        else:
            log.info("[%s] limited to versions %s", product.key, results.versions)

    for period, (start, end) in periods.items():
        fatal = _summary(api.report("appSummary", start, end, error_types=("FATAL",)))
        anr = _summary(api.report("appSummary", start, end, error_types=("ANR",)))
        both = _summary(api.report("appSummary", start, end, error_types=("FATAL", "ANR")))
        results.headline.append({
            "period": period,
            "crashes": _int(fatal.get("eventsCount")) or 0,
            "anrs": _int(anr.get("eventsCount")) or 0,
            "users_affected": _int(both.get("impactedUsersCount")) or 0,
        })
        # FATAL-only matches the console's "Crash-free users" card.
        active = next((_int(s.get("totalUsersCount")) for s in (fatal, both, anr)
                       if _int(s.get("totalUsersCount"))), 0)
        results.crash_free.append({
            "period": period,
            "active_users": active,
            "crashed_users": _int(fatal.get("impactedUsersCount")) or 0,
            "crash_free_users_pct": fatal.get("crashFreeUsersPercentage"),
        })

    start, end = periods["current"]
    rows = [_issue_row(g) for g in api.report("topIssues", start, end, page_size=50,
                                               error_types=("FATAL", "ANR"))]
    rows = [r for r in rows if r["issue_id"]]

    # One sample event per issue gives its exception class (for "By crash type") and top frames.
    for r in rows[:SAMPLE_LIMIT]:
        r.update(_sample_fields(api.latest_event(r["issue_id"], start, end)))

    detailed = _needs_detail(rows, top_n, max_flagged)
    for r in detailed:
        iid = r["issue_id"]
        # topVersions rejects a version filter (HTTP 400), so fetch all and keep the in-scope ones here.
        versions = _breakdown(api.report("topVersions", start, end, page_size=10, issue_id=iid, all_versions=True),
                              "version", "displayVersion")
        r["top_versions"] = [v for v in versions if not results.versions or v["value"] in results.versions][:3]
        r["top_devices"] = _breakdown(api.report("topAndroidDevices", start, end, page_size=3, issue_id=iid),
                                      "device", "displayName")
        r["top_os"] = _breakdown(api.report("topOperatingSystems", start, end, page_size=3, issue_id=iid),
                                 "operatingSystem", "displayVersion")

    # App-wide "By device model" (crashes + ANRs). Users here are real distinct users per device.
    results.devices = [
        {"device": (g.get("device") or {}).get("displayName"),
         "events": _int(_metrics(g).get("eventsCount")) or 0,
         "users": _int(_metrics(g).get("impactedUsersCount")) or 0}
        for g in api.report("topAndroidDevices", start, end, page_size=top_n, error_types=("FATAL", "ANR"))
    ]
    results.devices = [d for d in results.devices if d["events"] > 0]

    results.issues = rows
    log.info("[%s] %d issues in window, %d sampled, %d with details", product.key, len(rows),
             min(len(rows), SAMPLE_LIMIT), len(detailed))
    return results
