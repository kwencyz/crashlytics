"""Google credentials for the Crashlytics API, the Play Developer Reporting API and the GCS marker bucket.

Order:
1. GOOGLE_SERVICE_ACCOUNT_JSON env var holding a service-account key (JSON text, or base64 of it).
   Used by the Claude cloud routine, which has no gcloud login or key file.
2. Application Default Credentials (Cloud Run service account, GOOGLE_APPLICATION_CREDENTIALS,
   or `gcloud auth application-default login` locally).

Local user logins ignore the scopes asked for here. For the Play section, log in with:
  gcloud auth application-default login \
    --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/playdeveloperreporting
"""
from __future__ import annotations

import base64
import json
import os

SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
PLAY_REPORTING_SCOPE = "https://www.googleapis.com/auth/playdeveloperreporting"
ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"

_cached: dict[tuple[str, ...], object] = {}


def google_credentials(scopes: tuple[str, ...] = SCOPES):
    if scopes not in _cached:
        info = (os.environ.get(ENV_VAR) or "").strip()
        if info:
            from google.oauth2 import service_account

            if not info.startswith("{"):   # base64-encoded key: one line, no quotes, safe in .env fields
                info = base64.b64decode(info).decode("utf-8")
            _cached[scopes] = service_account.Credentials.from_service_account_info(
                json.loads(info), scopes=list(scopes))
        else:
            import google.auth

            _cached[scopes], _ = google.auth.default(scopes=list(scopes))
    return _cached[scopes]
