"""The API reader, against canned responses shaped like real Crashlytics v1alpha output."""
import json
from datetime import date

import pytest

from crash_digest.config import Product
from crash_digest.crashlytics_api import CrashlyticsAPIError, CrashlyticsClient, run_queries
from crash_digest.digest import build_digest
from crash_digest.windows import compute_windows

# Strings that must never make it out of the reader.
CANARIES = ["SECRET-MESSAGE", "SECRET-SUBTITLE", "SECRET-UUID", "SECRET-CRUMB", "SECRET-KEY"]

PRODUCT = Product(key="vt", enabled=True, display_name="Fiuu VT", gcp_project="demo-proj",
                  project_number="123", app_id="1:123:android:abc", teams_webhook_env="X")
WINDOWS = compute_windows(tz_name="Asia/Kuala_Lumpur", digest_hour=9, window_hours=24,
                          lookback_days=0, run_date=date(2026, 11, 18))
CUR_START = "2026-11-17T01:00:00Z"


class FakeCreds:
    valid, token = True, "t"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def summary(events, users, total=None, pct=None):
    m = {"eventsCount": str(events), "impactedUsersCount": str(users)}
    if total is not None:
        m["totalUsersCount"] = str(total)
    if pct is not None:
        m["crashFreeUsersPercentage"] = pct
    return {"groups": [{"metrics": [m]}]}


ISSUES = {"groups": [
    {"metrics": [{"eventsCount": "2", "impactedUsersCount": "1"}],
     "issue": {"id": "anr1", "errorType": "ANR", "title": "SourceFile - Sdk.a",
               "subtitle": "SECRET-SUBTITLE", "signals": [{"signal": "SIGNAL_REGRESSED"}]}},
    {"metrics": [{"eventsCount": "1", "impactedUsersCount": "1"}],
     "issue": {"id": "crash1", "errorType": "FATAL", "subtitle": "SECRET-SUBTITLE",
               "signals": [{"signal": "SIGNAL_FRESH"}, {"signal": "SIGNAL_REPETITIVE"}]}},
]}

EVENTS = {
    "crash1": {"events": [{
        "installationUuid": "SECRET-UUID", "breadcrumbs": [{"title": "SECRET-CRUMB"}],
        "customKeys": {"k": "SECRET-KEY"},
        "blameFrame": {"symbol": "Sdk.a", "file": "SourceFile", "line": "57"},
        "exceptions": [
            {"type": "java.lang.RuntimeException", "exceptionMessage": "SECRET-MESSAGE",
             "frames": [{"symbol": "Outer.run", "file": "Outer.java", "line": "1"}]},
            {"type": "android.os.DeadObjectException", "exceptionMessage": "SECRET-MESSAGE",
             "frames": [{"symbol": "Sdk.a", "file": "SourceFile", "line": "57", "library": "x"}]},
        ],
    }]},
    "anr1": {"events": [{
        "blameFrame": {"symbol": "Sdk.a", "file": "SourceFile", "line": "57"},
        "threads": [{"name": "other", "frames": [{"symbol": "Other.x"}]},
                    {"name": "main", "blamed": True, "frames": [{"symbol": f"Main.f{i}"} for i in range(9)]}],
    }]},
}


class FakeSession:
    def __init__(self, fail_first=0, status=503):
        self.calls, self.fail_first, self.status = [], fail_first, status

    def get(self, url, params, headers, timeout):
        self.calls.append((url, params, headers))
        if len(self.calls) <= self.fail_first:
            return FakeResp(self.status, {"error": "nope"})
        p = {}
        for k, v in params:
            p.setdefault(k, []).append(v)
        path = url.split("/apps/1:123:android:abc")[1]
        current = p["filter.interval.start_time"] == [CUR_START]
        types = tuple(p.get("filter.issue.error_types", []))
        iid = (p.get("filter.issue.id") or [None])[0]
        if path == "/reports/appSummary":
            if types == ("FATAL",):
                return FakeResp(200, summary(3, 2, 395, 99.49) if current else summary(1, 1, 380, 99.7))
            if types == ("ANR",):
                return FakeResp(200, summary(2, 1) if current else summary(0, 0))
            return FakeResp(200, summary(5, 3, 395) if current else summary(1, 1, 380))
        if path == "/reports/topIssues":
            return FakeResp(200, ISSUES)
        if path == "/reports/topVersions" and p.get("filter.version.display_names"):
            return FakeResp(400, {"error": {"message": "Request contains an invalid argument."}})  # like the real API
        if path == "/reports/topVersions" and iid is None:   # "which versions exist" lookup
            return FakeResp(200, {"groups": [
                {"version": {"displayVersion": dv, "displayName": dn}, "metrics": [{"eventsCount": "1"}]}
                for dv, dn in [("3.5.9", "3.5.9 (1)"), ("3.5.13", "3.5.13 (1)"), ("3.5.10", "3.5.10 (1)"),
                               ("3.5.14", "3.5.14 (2)"), ("3.5.13", "3.5.13 (2)"), ("3.4.30", "3.4.30 (1)")]]})
        if path == "/reports/topVersions":
            return FakeResp(200, {"groups": [{"version": {"displayVersion": "3.5.14"}, "metrics": [{"eventsCount": "2"}]},
                                             {"version": {"displayVersion": "3.4.25"}, "metrics": [{"eventsCount": "1"}]},
                                             {"version": {"displayVersion": "3.5.8"}, "metrics": [{"eventsCount": "0"}]}]})
        if path == "/reports/topAndroidDevices":
            return FakeResp(200, {"groups": [{"device": {"displayName": "vivo (V2339)"}, "metrics": [{"eventsCount": "2"}]}]})
        if path == "/reports/topOperatingSystems":
            return FakeResp(200, {"groups": [{"operatingSystem": {"displayVersion": "16"}, "metrics": [{"eventsCount": "2"}]}]})
        if path == "/events":
            return FakeResp(200, EVENTS[iid])
        return FakeResp(404, {})


def _run(session):
    client = CrashlyticsClient(PRODUCT, session=session, credentials=FakeCreds(), backoff_s=0)
    return run_queries(PRODUCT, WINDOWS, top_n=5, max_flagged=5, client=client)


def test_headline_and_crash_free():
    r = _run(FakeSession())
    cur = next(h for h in r.headline if h["period"] == "current")
    assert cur == {"period": "current", "crashes": 3, "anrs": 2, "users_affected": 3}
    d = build_digest(r.headline, r.crash_free, r.issues)
    assert d.current.crash_free_pct == 99.49 and d.previous.crash_free_pct == 99.7
    assert d.current.active_users == 395 and not d.looks_like_no_data


def test_issue_rows_and_signals():
    issues = {i["issue_id"]: i for i in _run(FakeSession()).issues}
    crash, anr = issues["crash1"], issues["anr1"]
    assert crash["is_new"] and not crash["is_returning"]
    assert anr["is_returning"] and not anr["is_new"]
    assert crash["exception_type"] == "android.os.DeadObjectException"     # root cause
    assert crash["frames"] == [{"symbol": "Sdk.a", "file": "SourceFile", "line": 57}]
    assert (crash["blame_file"], crash["blame_line"]) == ("SourceFile", 57)
    assert crash["top_devices"] == [{"value": "vivo (V2339)", "count": 2}]
    assert crash["top_versions"] == [{"value": "3.5.14", "count": 2}, {"value": "3.4.25", "count": 1}]  # 0s dropped
    assert anr["exception_type"] is None
    assert [f["symbol"] for f in anr["frames"]] == [f"Main.f{i}" for i in range(5)]  # blamed thread, top 5


def test_sensitive_fields_never_copied():
    text = json.dumps(_run(FakeSession()).__dict__)
    for c in CANARIES:
        assert c not in text


def test_quota_project_header_sent():
    s = FakeSession()
    _run(s)
    assert all(h["x-goog-user-project"] == "123" for _, _, h in s.calls)


def _version_filter(params):
    return {v for k, v in params if k == "filter.version.display_names"}


def test_all_versions_by_default():
    s = FakeSession()
    r = _run(s)
    assert r.versions == []
    assert all(not _version_filter(p) for _, p, _ in s.calls)


def test_latest_versions_filter_applied_to_every_call():
    product = Product(**{**PRODUCT.__dict__, "latest_versions": 2})
    s = FakeSession()
    client = CrashlyticsClient(product, session=s, credentials=FakeCreds(), backoff_s=0)
    r = run_queries(product, WINDOWS, client=client)
    assert r.versions == ["3.5.14", "3.5.13"]            # semver order, not text order
    lookup, rest = s.calls[0], s.calls[1:]
    assert lookup[0].endswith("/reports/topVersions") and not _version_filter(lookup[1])
    filtered = [p for u, p, _ in rest if not u.endswith("/reports/topVersions")]
    assert filtered and all(_version_filter(p) == {"3.5.14 (2)", "3.5.13 (1)", "3.5.13 (2)"} for p in filtered)
    crash = next(i for i in r.issues if i["issue_id"] == "crash1")
    assert crash["top_versions"] == [{"value": "3.5.14", "count": 2}]   # out-of-scope 3.4.25 dropped


def test_retries_on_503_then_succeeds():
    s = FakeSession(fail_first=2, status=503)
    _run(s)


def test_does_not_retry_403():
    s = FakeSession(fail_first=1, status=403)
    with pytest.raises(CrashlyticsAPIError, match="HTTP 403"):
        _run(s)
    assert len(s.calls) == 1
