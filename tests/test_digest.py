import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from crash_digest.ai import validate
from crash_digest.card import build_card
from crash_digest.config import Product
from crash_digest.digest import build_digest
from crash_digest.payload import build_ai_payload, scrub
from crash_digest.windows import compute_windows

FIXTURE = Path(__file__).parent / "fixtures" / "sample_rows.json"
FORBIDDEN = ["custom_keys", "logs", "breadcrumbs", "user", "email", "name", "installation_uuid",
             "exception_message", "subtitle", "message", "params", "value"]


@pytest.fixture
def product():
    return Product(key="vt", enabled=True, display_name="Fiuu VT", gcp_project="demo-proj",
                   project_number="123", app_id="1:123:android:abc",
                   teams_webhook_env="TEAMS_WEBHOOK_URL_VT")


@pytest.fixture
def digest():
    raw = json.loads(FIXTURE.read_text())
    return build_digest(raw["headline"], raw["crash_free"], raw["issues"])


def _all_keys(obj, acc=None):
    acc = set() if acc is None else acc
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k.lower())
            _all_keys(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _all_keys(v, acc)
    return acc


# ---------- windows ----------

def test_window_ends_at_9am_local():
    w = compute_windows(tz_name="Asia/Kuala_Lumpur", digest_hour=9, window_hours=24,
                        lookback_days=30, run_date=date(2026, 11, 18))
    assert w.window_end == datetime(2026, 11, 18, 1, 0, tzinfo=timezone.utc)   # 09:00 MYT = 01:00 UTC
    assert (w.window_end - w.window_start).total_seconds() == 24 * 3600
    assert (w.window_start - w.history_start).days == 30


def test_window_lag_for_batch_only():
    w = compute_windows(tz_name="Asia/Kuala_Lumpur", digest_hour=9, window_hours=24,
                        lookback_days=30, run_date=date(2026, 11, 18), lag_days=1)
    assert w.window_end == datetime(2026, 11, 17, 1, 0, tzinfo=timezone.utc)
    assert w.digest_date == date(2026, 11, 18)


# ---------- digest ----------

def test_digest_numbers(digest):
    assert digest.current.crashes == 142
    assert digest.current.crash_free_pct == pytest.approx(98.45, abs=0.01)
    assert [i.issue_id for i in digest.new_issues] == ["a1b2c3d4e5f60718293a4b5c6d7e8f90"]
    assert len(digest.returning_issues) == 1
    assert digest.issues[0].users >= digest.issues[1].users


def test_reported_crash_free_pct_wins():
    d = build_digest([], [{"period": "current", "active_users": 395, "crashed_users": 2,
                           "crash_free_users_pct": 99.74683}], [])
    assert d.current.crash_free_pct == 99.75


def test_no_data_vs_clean_day():
    no_data = build_digest([], [], [])
    assert no_data.looks_like_no_data
    clean = build_digest([], [{"period": "current", "active_users": 4000, "crashed_users": 0}], [])
    assert not clean.looks_like_no_data and not clean.has_any_events


# ---------- payload (what Claude sees) ----------

def test_payload_is_allow_listed(digest):
    payload = build_ai_payload(digest, product_name="Fiuu VT", window_label="x", top_n=5, max_flagged=5)
    keys = _all_keys(payload)
    assert not keys & set(FORBIDDEN), keys & set(FORBIDDEN)
    assert not any("installation" in k or "email" in k or "custom" in k for k in keys)


def test_payload_scrubs_card_like_numbers(digest):
    payload = build_ai_payload(digest, product_name="Fiuu VT", window_label="x", top_n=5, max_flagged=5)
    text = json.dumps(payload)
    assert "4111 1111 1111 1111" not in text
    assert "[number]" in text


def test_scrub():
    assert scrub("mail me at a.b@fiuu.com") == "mail me at [email]"
    assert "[uuid]" in scrub("id 123e4567-e89b-12d3-a456-426614174000")
    assert scrub("ReceiptPrinter.kt") == "ReceiptPrinter.kt"


# ---------- AI validation ----------

def test_ai_drops_invented_issue_ids(digest):
    payload = build_ai_payload(digest, product_name="Fiuu VT", window_label="x", top_n=5, max_flagged=5)
    real = payload["issues"][0]["issue_id"]
    t = validate({"summary": "Worse day.", "priorities": [
        {"issue_id": real, "reason": "New in 3.4.2 on SUNMI P2."},
        {"issue_id": "made-up", "reason": "?"}]}, payload)
    assert [p["issue_id"] for p in t.priorities] == [real]
    assert t.dropped == ["made-up"]


def test_ai_empty_summary_is_rejected(digest):
    payload = build_ai_payload(digest, product_name="Fiuu VT", window_label="x", top_n=5, max_flagged=5)
    assert validate({"summary": "  ", "priorities": []}, payload) is None


# ---------- card ----------

def _card_text(card):
    return json.dumps(card, ensure_ascii=False)


def test_card_without_ai_still_has_numbers(digest, product):
    card = build_card(digest, product=product, window_label="w", top_n=5, max_flagged=5,
                      triage=None, ai_enabled=True)
    text = _card_text(card)
    assert card["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert "AI summary unavailable" in text
    assert "142" in text and "98.45%" in text
    assert "ReceiptPrinter.kt:118" in text
    assert "console.firebase.google.com/v1/appid/project/demo-proj/crashlytics/app/1:123:android:abc/issues/" in text


def test_card_no_data_warning(product):
    card = build_card(build_digest([], [], []), product=product, window_label="w", top_n=5,
                      max_flagged=5, triage=None, ai_enabled=True)
    assert "No data received" in _card_text(card)
