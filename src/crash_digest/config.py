"""Load and validate config.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Settings:
    timezone: str
    digest_hour: int
    window_hours: int
    top_n: int
    max_flagged: int
    ai_model: str
    ai_max_tokens: int
    state_location: str


@dataclass(frozen=True)
class Product:
    key: str
    enabled: bool
    display_name: str
    gcp_project: str       # project ID (text), used for console links
    project_number: str    # project number (digits), used by the Crashlytics API
    app_id: str            # Firebase app ID, e.g. 1:123456789012:android:abc123
    teams_webhook_env: str
    latest_versions: int = 0   # >0: only count the newest N app versions; 0: all versions

    def issue_url(self, issue_id: str) -> str:
        # Same link format the Crashlytics API returns in issue.uri.
        return (
            f"https://console.firebase.google.com/v1/appid/project/{self.gcp_project}"
            f"/crashlytics/app/{self.app_id}/issues/{issue_id}"
        )

    def webhook_url(self) -> str | None:
        return os.environ.get(self.teams_webhook_env)


@dataclass(frozen=True)
class Config:
    settings: Settings
    products: dict[str, Product]


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("CRASH_DIGEST_CONFIG", "config.yaml"))
    raw = yaml.safe_load(path.read_text())
    settings = Settings(**raw["settings"])
    products = {
        key: Product(key=key, **{**value, "project_number": str(value.get("project_number", "")),
                                 "app_id": str(value.get("app_id", ""))})
        for key, value in (raw.get("products") or {}).items()
    }
    return Config(settings=settings, products=products)


def check_product_ready(product: Product) -> list[str]:
    """Return a list of problems that would stop a real (non-fixture) run."""
    problems = []
    for field in ("gcp_project", "project_number", "app_id"):
        if getattr(product, field) in ("", "CHANGE_ME", None):
            problems.append(f"products.{product.key}.{field} is not set in config.yaml")
    return problems
