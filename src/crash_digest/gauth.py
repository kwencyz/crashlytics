"""Google credentials for the Crashlytics API and the GCS marker bucket.

Order:
1. GOOGLE_SERVICE_ACCOUNT_JSON env var holding a service-account key (JSON text, or base64 of it).
   Used by the Claude cloud routine, which has no gcloud login or key file.
2. Application Default Credentials (Cloud Run service account, GOOGLE_APPLICATION_CREDENTIALS,
   or `gcloud auth application-default login` locally).
"""
from __future__ import annotations

import base64
import json
import os

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"

_cached = None


def google_credentials():
    global _cached
    if _cached is None:
        info = (os.environ.get(ENV_VAR) or "").strip()
        if info:
            from google.oauth2 import service_account

            if not info.startswith("{"):   # base64-encoded key: one line, no quotes, safe in .env fields
                info = base64.b64decode(info).decode("utf-8")
            _cached = service_account.Credentials.from_service_account_info(json.loads(info), scopes=SCOPES)
        else:
            import google.auth

            _cached, _ = google.auth.default(scopes=SCOPES)
    return _cached
