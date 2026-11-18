"""End-to-end run with the Crashlytics API replaced by the fixture, and Claude/Teams mocked."""
import json
from pathlib import Path

import pytest
import yaml

import crash_digest.__main__ as cli
from crash_digest.ai import Triage
from crash_digest.teams import TeamsPostError

FIXTURE = str(Path(__file__).parent / "fixtures" / "sample_rows.json")


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    cfg = yaml.safe_load((Path(__file__).parents[1] / "config.yaml").read_text())
    cfg["settings"]["state_location"] = str(tmp_path / "markers")
    cfg["products"]["vt"].update(gcp_project="demo-proj", project_number="123", app_id="1:123:android:abc")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setenv("TEAMS_WEBHOOK_URL_VT", "https://example.invalid/webhook")
    return str(path)


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "post_card", lambda url, msg, **kw: sent.append(msg))
    return sent


def _args(cfg_path, *extra):
    return ["--config", cfg_path, "--product", "vt", "--fixture", FIXTURE, "--date", "2026-11-18", *extra]


def test_posts_once_then_skips(cfg_path, posts, monkeypatch):
    real_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    monkeypatch.setattr(cli, "write_triage", lambda payload, **kw: Triage(
        summary="Worse day, driven by a new printer crash.",
        priorities=[{"issue_id": real_id, "reason": "New in 3.4.2, almost all on SUNMI P2."}]))
    assert cli.main(_args(cfg_path)) == 0
    assert cli.main(_args(cfg_path)) == 0          # second run same day: skipped
    assert len(posts) == 1
    text = json.dumps(posts[0], ensure_ascii=False)
    assert "Look at first" in text and "SUNMI P2" in text
    assert cli.main(_args(cfg_path, "--force")) == 0
    assert len(posts) == 2


def test_ai_failure_still_posts(cfg_path, posts, monkeypatch):
    monkeypatch.setattr(cli, "write_triage", lambda payload, **kw: None)
    assert cli.main(_args(cfg_path)) == 0
    assert "AI summary unavailable" in json.dumps(posts[0], ensure_ascii=False)


def test_teams_failure_fails_job_and_does_not_mark(cfg_path, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "write_triage", lambda payload, **kw: None)

    def boom(url, msg, **kw):
        raise TeamsPostError("HTTP 404")
    monkeypatch.setattr(cli, "post_card", boom)
    assert cli.main(_args(cfg_path)) == 1
    assert not list((tmp_path / "markers").rglob("*.posted"))


def test_query_failure_posts_error_card(cfg_path, posts, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("table not found")
    monkeypatch.setattr(cli, "run_queries", broken)
    args = [a for a in _args(cfg_path) if a not in ("--fixture", FIXTURE)]
    assert cli.main(args) == 1
    assert "Digest failed" in json.dumps(posts[0], ensure_ascii=False)


def test_routine_flow_dry_run_then_post_with_triage_file(cfg_path, posts, monkeypatch, tmp_path):
    """What the Claude routine does: dry run → write triage.json → post saved results."""
    def no_api(*a, **kw):
        raise AssertionError("routine flow must not call the Claude API")
    monkeypatch.setattr(cli, "write_triage", no_api)
    out = tmp_path / "out"
    assert cli.main(_args(cfg_path, "--dry-run", "--out", str(out))) == 0
    results = out / "vt_results.json"
    assert results.exists() and (out / "vt_payload.json").exists() and posts == []

    real_id = json.loads((out / "vt_payload.json").read_text())["issues"][0]["issue_id"]
    triage = tmp_path / "triage.json"
    triage.write_text(json.dumps({"summary": "Worse day, new printer crash.", "priorities": [
        {"issue_id": real_id, "reason": "New in 3.4.2 on SUNMI P2."},
        {"issue_id": "invented", "reason": "?"}, "not-a-dict"]}))
    args = ["--config", cfg_path, "--product", "vt", "--fixture", str(results), "--date", "2026-11-18",
            "--triage-file", str(triage)]
    assert cli.main(args) == 0
    text = json.dumps(posts[0], ensure_ascii=False)
    assert "Look at first" in text and "new printer crash" in text and "invented" not in text


def test_bad_triage_file_still_posts(cfg_path, posts, tmp_path):
    bad = tmp_path / "triage.json"
    bad.write_text("not json")
    assert cli.main(_args(cfg_path, "--triage-file", str(bad))) == 0
    assert "AI summary unavailable" in json.dumps(posts[0], ensure_ascii=False)


def test_markers_off_posts_every_run(cfg_path, posts, monkeypatch, tmp_path):
    cfg = yaml.safe_load(Path(cfg_path).read_text())
    cfg["settings"]["state_location"] = "none"
    Path(cfg_path).write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(cli, "write_triage", lambda payload, **kw: None)
    assert cli.main(_args(cfg_path)) == 0
    assert cli.main(_args(cfg_path)) == 0
    assert len(posts) == 2


def test_dry_run_never_posts(cfg_path, posts, capsys):
    assert cli.main(_args(cfg_path, "--dry-run")) == 0
    assert posts == []
    out = capsys.readouterr().out
    assert "PAYLOAD SENT TO CLAUDE" in out and "4111 1111" not in out
