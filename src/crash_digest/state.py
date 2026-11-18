"""'Already posted today' markers so a retry or manual re-run never double-posts.

state_location is either gs://bucket/prefix (production) or a local folder (dev).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path


def _key(product_key: str, digest_date: date) -> str:
    return f"{product_key}/{digest_date.isoformat()}.posted"


class MarkerStore:
    def __init__(self, location: str):
        self.location = location
        self._gcs = location.startswith("gs://")
        if self._gcs:
            bucket, _, prefix = location[5:].partition("/")
            self._bucket_name, self._prefix = bucket, prefix.strip("/")
        else:
            Path(location).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._gcs and self._prefix else key

    def _blob(self, key: str):
        from google.cloud import storage

        from .gauth import google_credentials

        creds = google_credentials()
        client = storage.Client(credentials=creds, project=getattr(creds, "project_id", None))
        return client.bucket(self._bucket_name).blob(self._path(key))

    def already_posted(self, product_key: str, digest_date: date) -> bool:
        key = _key(product_key, digest_date)
        if self._gcs:
            return self._blob(key).exists()
        return (Path(self.location) / key).exists()

    def mark_posted(self, product_key: str, digest_date: date) -> None:
        key = _key(product_key, digest_date)
        stamp = datetime.now(timezone.utc).isoformat()
        if self._gcs:
            self._blob(key).upload_from_string(stamp)
        else:
            path = Path(self.location) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(stamp)
