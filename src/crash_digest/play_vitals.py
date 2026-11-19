"""Read Android vitals from the Google Play Developer Reporting API (v1beta1).

These are the same calls the play-console-mcp server (github.com/OrellBuehler/play-console-mcp)
makes: metric-set freshness, crashRateMetricSet / anrRateMetricSet `:query`, and `errorIssues:search`.

Play data is different from Crashlytics in two ways the card has to respect:
- It is daily, in America/Los_Angeles days, and lags about a day. So this section reports the
  latest complete Play day, not the card's 24h window.
- Play only counts users who opted in to sharing usage data, and its rates are the ones Google
  uses for "bad behavior" (lower visibility on the store). They won't match Crashlytics counts.

Privacy: every value we keep is copied by name. Raw error reports (full stack text) are never
fetched. An issue's `cause` is cut down to the exception class name and dropped if it doesn't
look like one, so exception messages can't reach the card.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://playdeveloperreporting.googleapis.com/v1beta1"
RETRYABLE = {429, 500, 502, 503, 504}
PLAY_TZ = "America/Los_Angeles"     # daily metric queries only accept this time zone
SEARCH_TZ = "UTC"                   # ...and the :search endpoints only accept UTC

# Google Play "bad behavior" thresholds, judged on the 28-day user-perceived rate (percent).
CRASH_THRESHOLD_PCT = 1.09
ANR_THRESHOLD_PCT = 0.47

ISSUE_PAGES = 2                     # up to 100 issues for one day is plenty for a top-N list

# Fully qualified only (java.lang.Foo): a bare first word is more likely the start of a message.
_CLASS_NAME = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)+$")
_LOCATION = re.compile(r"^[\w$.<>\-]+(\([\w$.\-]*(:\d+)?\))?$")


class PlayAPIError(RuntimeError):
    pass


class PlayClient:
    def __init__(self, package_name: str, quota_project: str, *, session=None, credentials=None,
                 attempts: int = 3, backoff_s: float = 2.0):
        self.package_name = package_name
        self._base = f"{API_ROOT}/apps/{package_name}"
        self._quota_project = str(quota_project)
        self._session = session or requests.Session()
        self._creds = credentials
        self._attempts = attempts
        self._backoff_s = backoff_s

    def _token(self) -> str:
        if self._creds is None:
            from .gauth import PLAY_REPORTING_SCOPE, google_credentials

            self._creds = google_credentials((PLAY_REPORTING_SCOPE,))
        if not self._creds.valid:
            from google.auth.transport.requests import Request

            self._creds.refresh(Request())
        return self._creds.token

    def _request(self, method: str, path: str, *, params=None, body=None) -> dict:
        last = None
        for attempt in range(1, self._attempts + 1):
            try:
                resp = self._session.request(
                    method,
                    self._base + path,
                    params=params,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._token()}",
                        # Bills user-credential calls to our project instead of gcloud's own.
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
            log.warning("Play %s %s attempt %d/%d failed: %s", method, path, attempt, self._attempts, last)
            if attempt < self._attempts:
                time.sleep(self._backoff_s * attempt)
        raise PlayAPIError(f"{method} {path} failed: {last}")

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body=body)


# ---------- small parsers ----------

def _day(value) -> date | None:
    """A google.type.DateTime dict, or an RFC 3339 string, as a calendar date."""
    if isinstance(value, dict):
        try:
            return date(int(value["year"]), int(value["month"]), int(value["day"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _number(entry: dict | None) -> float | None:
    if not entry:
        return None
    raw = (entry.get("decimalValue") or {}).get("value")
    if raw is None:
        raw = entry.get("int64Value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pct(fraction: float | None) -> float | None:
    """Play returns rates as fractions (0.0042); the card shows percent (0.42)."""
    return None if fraction is None else round(fraction * 100, 3)


def _date_body(d: date, tz: str) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day, "timeZone": {"id": tz}}


def _cause(raw) -> str | None:
    """Exception class only. Anything after the class name (a message) is cut off."""
    if not isinstance(raw, str):
        return None
    head = re.split(r"[:\s]", raw.strip(), maxsplit=1)[0]
    return head if _CLASS_NAME.match(head) and len(head) <= 200 else None


def _location(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw if _LOCATION.match(raw) and len(raw) <= 200 else None


def _console_url(raw) -> str | None:
    return raw if isinstance(raw, str) and raw.startswith("https://play.google.com/console/") else None


# ---------- queries ----------

def latest_day(client: PlayClient) -> date | None:
    """Last complete Play day for crash data.

    freshness latestEndTime is exclusive (ready to reuse as a timelineSpec end), so the last day
    with data is the day before it.
    """
    body = client.get("/crashRateMetricSet")
    for f in (body.get("freshnessInfo") or {}).get("freshnesses") or []:
        if f.get("aggregationPeriod") == "DAILY":
            end = _day(f.get("latestEndTime"))
            return end - timedelta(days=1) if end else None
    return None


def _rate_rows(client: PlayClient, metric_set: str, metrics: list[str], day: date) -> dict[date, dict]:
    """Daily rows for `day` and the day before, keyed by date, as {metric: number}."""
    body = client.post(f"/{metric_set}:query", {
        "timelineSpec": {
            "aggregationPeriod": "DAILY",
            "startTime": _date_body(day - timedelta(days=1), PLAY_TZ),
            "endTime": _date_body(day + timedelta(days=1), PLAY_TZ),   # exclusive
        },
        "metrics": metrics,
        "pageSize": 10,
    })
    out = {}
    for row in body.get("rows") or []:
        d = _day(row.get("startTime"))
        if d:
            out[d] = {m.get("metric"): _number(m) for m in row.get("metrics") or [] if m.get("metric")}
    return out


def _rate_block(rows: dict[date, dict], day: date, rate: str, rate_28d: str) -> dict | None:
    cur, prev = rows.get(day), rows.get(day - timedelta(days=1))
    if not cur:
        return None   # too few users that day, or Google hasn't published it
    users = cur.get("distinctUsers")
    return {
        "rate": _pct(cur.get(rate)),
        "prev_rate": _pct(prev.get(rate)) if prev else None,
        "rate_28d": _pct(cur.get(rate_28d)),
        "users": int(users) if users is not None else None,
    }


def _issues(client: PlayClient, day: date) -> list[dict]:
    params = {
        "interval.startTime.year": day.year, "interval.startTime.month": day.month,
        "interval.startTime.day": day.day, "interval.startTime.timeZone.id": SEARCH_TZ,
        "interval.endTime.year": (day + timedelta(days=1)).year,
        "interval.endTime.month": (day + timedelta(days=1)).month,
        "interval.endTime.day": (day + timedelta(days=1)).day,
        "interval.endTime.timeZone.id": SEARCH_TZ,
        "pageSize": 50,
    }
    raw: list[dict] = []
    for _ in range(ISSUE_PAGES):
        body = client.get("/errorIssues:search", params)
        raw += body.get("errorIssues") or []
        token = body.get("nextPageToken")
        if not token:
            break
        params = {**params, "pageToken": token}

    rows = []
    for i in raw:
        kind = i.get("type")
        if kind not in ("CRASH", "ANR"):   # NON_FATAL isn't a crash
            continue
        name = i.get("name") or ""
        rows.append({
            "issue_id": name.rsplit("/", 1)[-1] or None,
            "type": kind,
            "cause": _cause(i.get("cause")),
            "location": _location(i.get("location")),
            "users": _int(i.get("distinctUsers")),
            "reports": _int(i.get("errorReportCount")),
            "last_version": str(_int((i.get("lastAppVersion") or {}).get("versionCode")) or "") or None,
            "url": _console_url(i.get("issueUri")),
        })
    rows = [r for r in rows if r["issue_id"] and r["reports"] > 0]
    rows.sort(key=lambda r: (r["users"], r["reports"]), reverse=True)
    return rows


def run_play_queries(package_name: str, quota_project: str, *, client: PlayClient | None = None) -> dict:
    """App-wide (all versions) Play vitals for the latest complete Play day."""
    api = client or PlayClient(package_name, quota_project)
    day = latest_day(api)
    if day is None:
        raise PlayAPIError("crashRateMetricSet has no DAILY freshness; is the app published?")

    crash_rows = _rate_rows(api, "crashRateMetricSet",
                            ["userPerceivedCrashRate", "userPerceivedCrashRate28dUserWeighted",
                             "distinctUsers"], day)
    anr_rows = _rate_rows(api, "anrRateMetricSet",
                          ["userPerceivedAnrRate", "userPerceivedAnrRate28dUserWeighted",
                           "distinctUsers"], day)
    result = {
        "package": package_name,
        "day": day.isoformat(),
        "crash": _rate_block(crash_rows, day, "userPerceivedCrashRate", "userPerceivedCrashRate28dUserWeighted"),
        "anr": _rate_block(anr_rows, day, "userPerceivedAnrRate", "userPerceivedAnrRate28dUserWeighted"),
        "issues": _issues(api, day),
    }
    log.info("[play %s] day %s, %d crash/ANR issues", package_name, result["day"], len(result["issues"]))
    return result
