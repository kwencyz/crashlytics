"""Daily Crashlytics digest → Teams.

Examples
  python -m crash_digest --product vt                       # normal daily run (what Cloud Run executes)
  python -m crash_digest --product vt --dry-run             # read Crashlytics, print AI payload + card, post nothing
  python -m crash_digest --product vt --dry-run --with-ai   # same, but also call Claude
  python -m crash_digest --product vt --fixture tests/fixtures/sample_rows.json --dry-run --with-ai
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .ai import load_triage, write_triage
from .card import build_card, build_error_card
from .config import Config, Product, check_product_ready, load_config
from .crashlytics_api import QueryResults, run_queries
from .digest import build_digest
from .payload import build_ai_payload
from .state import MarkerStore
from .teams import TeamsPostError, post_card
from .windows import compute_windows

log = logging.getLogger("crash_digest")


def _load_fixture(path: str) -> QueryResults:
    raw = json.loads(Path(path).read_text())
    return QueryResults(headline=raw["headline"], crash_free=raw.get("crash_free"), issues=raw["issues"],
                        versions=raw.get("versions") or [])


def _dump(title: str, obj, out_dir: Path | None, filename: str) -> None:
    text = json.dumps(obj, indent=2, default=str, ensure_ascii=False)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / filename).write_text(text)
        print(f"wrote {out_dir / filename}")
    else:
        print(f"\n===== {title} =====\n{text}")


def run_product(cfg: Config, product: Product, args) -> bool:
    s = cfg.settings
    windows = compute_windows(
        tz_name=s.timezone,
        digest_hour=s.digest_hour,
        window_hours=s.window_hours,
        lookback_days=0,          # "new" and "regressed" come from Crashlytics signals now
        run_date=args.date,
    )
    label = windows.label(s.timezone)
    log.info("[%s] window %s", product.key, label)

    live = not args.dry_run
    webhook = product.webhook_url()
    if live and not webhook:
        log.error("[%s] %s is not set", product.key, product.teams_webhook_env)
        return False

    markers_on = live and str(s.state_location or "").lower() not in ("", "none")
    markers = MarkerStore(s.state_location) if markers_on else None
    if markers and not args.force and markers.already_posted(product.key, windows.digest_date):
        log.info("[%s] already posted for %s; skipping (use --force to re-post)", product.key, windows.digest_date)
        return True

    # 1. Numbers (Crashlytics API)
    try:
        if args.fixture:
            results = _load_fixture(args.fixture)
        else:
            problems = check_product_ready(product)
            if problems:
                raise RuntimeError("; ".join(problems))
            results = run_queries(product, windows, top_n=s.top_n, max_flagged=s.max_flagged)
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] query failed", product.key)
        if live:
            try:
                post_card(webhook, build_error_card(product, label, f"could not read crash data ({type(exc).__name__})."))
            except TeamsPostError:
                log.exception("[%s] could not post the error card either", product.key)
        return False

    if args.dry_run and args.out:
        # Saved so a later live run can post exactly these numbers: --fixture out/<key>_results.json
        _dump("RESULTS", results.__dict__, args.out, f"{product.key}_results.json")

    digest = build_digest(results.headline, results.crash_free, results.issues, results.versions)

    # 2. What Claude is allowed to see
    payload = build_ai_payload(digest, product_name=product.display_name, window_label=label,
                               top_n=s.top_n, max_flagged=s.max_flagged)
    if args.dry_run:
        _dump("PAYLOAD SENT TO CLAUDE (allow-listed)", payload, args.out, f"{product.key}_payload.json")

    # 3. Words (AI)
    ai_enabled = not args.no_ai
    ai_shown = ai_enabled and (live or args.with_ai or bool(args.triage_file))
    triage = None
    if ai_enabled and digest.has_any_events:
        if args.triage_file:          # written by the Claude routine
            triage = load_triage(args.triage_file, payload)
        elif live or args.with_ai:    # Claude API call
            triage = write_triage(payload, model=s.ai_model, max_tokens=s.ai_max_tokens)

    # 4. Card
    card = build_card(digest, product=product, window_label=label, top_n=s.top_n,
                      max_flagged=s.max_flagged, triage=triage, ai_enabled=ai_shown)
    if args.dry_run:
        _dump("TEAMS CARD", card, args.out, f"{product.key}_card.json")
        return True

    try:
        post_card(webhook, card)
    except TeamsPostError:
        log.exception("[%s] Teams post failed", product.key)
        return False
    if markers:
        markers.mark_posted(product.key, windows.digest_date)
    return True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="crash_digest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--product", action="append", help="product key from config (repeatable); default: all enabled")
    p.add_argument("--date", type=date.fromisoformat, help="digest date YYYY-MM-DD (default: today, local)")
    p.add_argument("--dry-run", action="store_true", help="print payload and card; never post or write markers")
    p.add_argument("--with-ai", action="store_true", help="in --dry-run, also call Claude")
    p.add_argument("--no-ai", action="store_true", help="skip the AI section entirely")
    p.add_argument("--triage-file", help="use this JSON {summary, priorities} instead of calling the Claude API")
    p.add_argument("--fixture", help="JSON file of rows to use instead of calling Crashlytics")
    p.add_argument("--force", action="store_true", help="post even if already posted today")
    p.add_argument("--out", type=Path, help="with --dry-run, write JSON files here instead of printing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    keys = args.product or [k for k, v in cfg.products.items() if v.enabled]
    unknown = [k for k in keys if k not in cfg.products]
    if unknown:
        log.error("unknown product(s): %s", unknown)
        return 2

    ok = all([run_product(cfg, cfg.products[k], args) for k in keys])
    return 0 if ok else 1   # non-zero fails the Cloud Run job → Cloud Monitoring alert


if __name__ == "__main__":
    sys.exit(main())
