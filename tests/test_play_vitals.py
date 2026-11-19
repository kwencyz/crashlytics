"""The Play Developer Reporting reader and the card's Play section, against canned responses."""
import json
from pathlib import Path

import pytest
import yaml

import crash_digest.__main__ as cli
from crash_digest.card import build_card
from crash_digest.config import Product
from crash_digest.digest import build_digest
from crash_digest.play_vitals import PlayAPIError, PlayClient, run_play_queries

PKG = "com.example.vt"
BASE = f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{PKG}"
FIXTURE = Path(__file__).parent / "fixtures" / "sample_rows.json"


class FakeCreds:
    valid, token = True, "t"


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def row(day, metrics):
    return {"startTime": {"year": 2026, "month": 11, "day": day, "timeZone": {"id": "America/Los_Angeles"}},
            "metrics": [{"metric": k, "decimalValue": {"value": str(v)}} for k, v in metrics.items()]}


ISSUES_PAGE_1 = {"errorIssues": [
    {"name": f"apps/{PKG}/errorIssues/small", "type": "CRASH",
     "cause": "java.lang.IllegalStateException: SECRET-MESSAGE card 4111 1111",
     "location": "com.example.Foo.bar", "errorReportCount": "3", "distinctUsers": "1",
     "lastAppVersion": {"versionCode": "2026111301"},
     "issueUri": "https://play.google.com/console/developers/1/app/2/vitals/crashes/small/details"},
    {"name": f"apps/{PKG}/errorIssues/nonfatal", "type": "NON_FATAL", "cause": "x.Y",
     "errorReportCount": "99", "distinctUsers": "99"},
], "nextPageToken": "p2"}

ISSUES_PAGE_2 = {"errorIssues": [
    {"name": f"apps/{PKG}/errorIssues/big", "type": "ANR", "cause": "Input dispatching timed out SECRET-MESSAGE",
     "location": "android.os.MessageQueue.nativePollOnce", "errorReportCount": "10", "distinctUsers": "7",
     "issueUri": "https://evil.example/phish"},
]}


class FakeSession:
    def __init__(self, fail_first=0, status=503, crash_rows=None):
        self.calls, self.fail_first, self.status = [], fail_first, status
        self.crash_rows = crash_rows

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append((method, url, params, json, headers))
        if len(self.calls) <= self.fail_first:
            return FakeResp(self.status, {"error": "nope"})
        path = url[len(BASE):]
        if method == "GET" and path == "/crashRateMetricSet":
            # Exclusive end: data runs through 17 Nov.
            return FakeResp(200, {"freshnessInfo": {"freshnesses": [
                {"aggregationPeriod": "HOURLY", "latestEndTime": {"year": 2026, "month": 11, "day": 18, "hours": 5}},
                {"aggregationPeriod": "DAILY", "latestEndTime": {"year": 2026, "month": 11, "day": 18,
                                                                 "timeZone": {"id": "America/Los_Angeles"}}}]}})
        if method == "POST" and path == "/crashRateMetricSet:query":
            rows = self.crash_rows if self.crash_rows is not None else [
                row(16, {"userPerceivedCrashRate": 0.004, "userPerceivedCrashRate28dUserWeighted": 0.011,
                         "distinctUsers": 1200}),
                row(17, {"userPerceivedCrashRate": 0.0045, "userPerceivedCrashRate28dUserWeighted": 0.0112,
                         "distinctUsers": 1234})]
            return FakeResp(200, {"rows": rows})
        if method == "POST" and path == "/anrRateMetricSet:query":
            return FakeResp(200, {"rows": [
                row(17, {"userPerceivedAnrRate": 0.001, "userPerceivedAnrRate28dUserWeighted": 0.002,
                         "distinctUsers": 1234})]})
        if method == "GET" and path == "/errorIssues:search":
            return FakeResp(200, ISSUES_PAGE_2 if (params or {}).get("pageToken") == "p2" else ISSUES_PAGE_1)
        return FakeResp(404, {})


def _run(session):
    client = PlayClient(PKG, "123", session=session, credentials=FakeCreds(), backoff_s=0)
    return run_play_queries(PKG, "123", client=client)


def test_latest_full_day_and_rates():
    s = FakeSession()
    r = _run(s)
    assert r["day"] == "2026-11-17"                    # freshness end is exclusive
    assert r["crash"] == {"rate": 0.45, "prev_rate": 0.4, "rate_28d": 1.12, "users": 1234}
    assert r["anr"] == {"rate": 0.1, "prev_rate": None, "rate_28d": 0.2, "users": 1234}
    query = next(body for m, u, _, body, _ in s.calls if u.endswith("/crashRateMetricSet:query"))
    spec = query["timelineSpec"]
    assert spec["aggregationPeriod"] == "DAILY"
    assert (spec["startTime"]["day"], spec["endTime"]["day"]) == (16, 18)   # 16 and 17, end exclusive
    assert spec["startTime"]["timeZone"] == {"id": "America/Los_Angeles"}


def test_issues_sorted_paged_and_filtered():
    s = FakeSession()
    issues = _run(s)["issues"]
    assert [i["issue_id"] for i in issues] == ["big", "small"]          # by users; NON_FATAL dropped
    assert issues[1]["cause"] == "java.lang.IllegalStateException"     # message cut off
    assert issues[0]["cause"] is None                                   # "Input dispatching..." isn't a class
    assert issues[1]["last_version"] == "2026111301"
    assert issues[1]["url"].startswith("https://play.google.com/console/")
    assert issues[0]["url"] is None                                     # non-Play link dropped
    search = [p for m, u, p, _, _ in s.calls if u.endswith("/errorIssues:search")]
    assert len(search) == 2 and search[0]["interval.startTime.timeZone.id"] == "UTC"


def test_no_messages_or_card_numbers_copied():
    text = json.dumps(_run(FakeSession()))
    assert "SECRET-MESSAGE" not in text and "4111" not in text


def test_quota_project_header_sent():
    s = FakeSession()
    _run(s)
    assert all(h["x-goog-user-project"] == "123" for *_, h in s.calls)


def test_day_with_no_rows():
    r = _run(FakeSession(crash_rows=[]))
    assert r["crash"] is None and r["anr"] is not None


def test_retries_on_503_not_403():
    _run(FakeSession(fail_first=2, status=503))
    s = FakeSession(fail_first=1, status=403)
    with pytest.raises(PlayAPIError, match="HTTP 403"):
        _run(s)
    assert len(s.calls) == 1


# ---------- card ----------

@pytest.fixture
def product():
    return Product(key="vt", enabled=True, display_name="Fiuu VT", gcp_project="demo-proj",
                   project_number="123", app_id="1:123:android:abc", teams_webhook_env="X")


def _card(product, play, versions=None):
    raw = json.loads(FIXTURE.read_text())
    d = build_digest(raw["headline"], raw["crash_free"], raw["issues"], versions, None, play)
    return json.dumps(build_card(d, product=product, window_label="w", top_n=5, max_flagged=5,
                                 triage=None, ai_enabled=False), ensure_ascii=False)


def test_card_play_section(product):
    text = _card(product, _run(FakeSession()), versions=["3.5.14"])
    assert "Google Play vitals · 17 Nov" in text
    assert "0.45% (▲0.05 pts) · 28-day 1.12% ⚠️ over Play's 1.09% limit" in text
    assert "0.10% · 28-day 0.20%" in text and "0.20% ⚠️" not in text
    assert "IllegalStateException** · Crash" in text and "1,234" in text
    assert '"**ANR**"' in text and "**ANR** · ANR" not in text     # no class name: kind shown once
    assert "Crashlytics numbers" in text and "all numbers below" not in text
    assert "Numbers from Firebase Crashlytics and Google Play Console" in text


def test_card_without_play_unchanged(product):
    text = _card(product, None, versions=["3.5.14"])
    assert "Google Play" not in text and "all numbers below" in text


def test_card_play_error_note(product):
    text = _card(product, {"package": PKG, "error": "PlayAPIError"})
    assert "Play Console data unavailable today (PlayAPIError)" in text
    assert "and Google Play Console" not in text


# ---------- run ----------

def test_play_failure_still_posts_crashlytics_card(tmp_path, monkeypatch):
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text())
    cfg["settings"]["state_location"] = "none"
    cfg["products"]["vt"].update(gcp_project="demo-proj", project_number="123",
                                 app_id="1:123:android:abc", play_package_name=PKG)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("TEAMS_WEBHOOK_URL_VT", "https://example.invalid/webhook")

    results = cli._load_fixture(str(FIXTURE))
    monkeypatch.setattr(cli, "run_queries", lambda *a, **kw: results)

    def broken(*a, **kw):
        raise PlayAPIError("GET /crashRateMetricSet failed: HTTP 403")
    monkeypatch.setattr(cli, "run_play_queries", broken)
    monkeypatch.setattr(cli, "write_triage", lambda payload, **kw: None)
    sent = []
    monkeypatch.setattr(cli, "post_card", lambda url, msg, **kw: sent.append(msg))

    assert cli.main(["--config", str(path), "--product", "vt", "--date", "2026-11-18"]) == 0
    text = json.dumps(sent[0], ensure_ascii=False)
    assert "Play Console data unavailable today" in text and "Top 5 by users affected" in text
