"""Time windows for one digest run."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Windows:
    digest_date: date          # the local date the digest is "for" (used for the no-double-post marker)
    window_start: datetime     # UTC
    window_end: datetime       # UTC
    prev_window_start: datetime
    history_start: datetime

    def query_params(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "prev_window_start": self.prev_window_start,
            "history_start": self.history_start,
        }

    def label(self, tz_name: str) -> str:
        tz = ZoneInfo(tz_name)
        s = self.window_start.astimezone(tz)
        e = self.window_end.astimezone(tz)
        return f"{s:%d %b %H:%M} – {e:%d %b %H:%M} ({tz_name.split('/')[-1].replace('_', ' ')})"


def compute_windows(
    *,
    tz_name: str,
    digest_hour: int,
    window_hours: int,
    lookback_days: int,
    run_date: date | None = None,
    lag_days: int = 0,
    now: datetime | None = None,
) -> Windows:
    """The window ends at `digest_hour` local time on `run_date` (default: today, local).

    Re-running later the same day gives the same window, so numbers are stable.
    `lag_days` shifts the window back, used when only the daily batch table exists
    (batch data for "yesterday" is not complete at 09:00).
    """
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(timezone.utc)
    if run_date is None:
        run_date = now.astimezone(tz).date()
    end_local = datetime(run_date.year, run_date.month, run_date.day, digest_hour, tzinfo=tz)
    end_local -= timedelta(days=lag_days)
    window_end = end_local.astimezone(timezone.utc)
    window_start = window_end - timedelta(hours=window_hours)
    return Windows(
        digest_date=run_date,
        window_start=window_start,
        window_end=window_end,
        prev_window_start=window_start - timedelta(hours=window_hours),
        history_start=window_start - timedelta(days=lookback_days),
    )
