"""Turn raw API rows into the digest model. All numbers on the card come from here (Crashlytics), never from the AI."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Issue:
    issue_id: str
    error_type: str            # FATAL | ANR
    events: int
    users: int
    is_new: bool
    is_returning: bool
    top_versions: list[tuple[str, int]]
    top_devices: list[tuple[str, int]]
    top_os: list[tuple[str, int]]
    exception_type: str | None
    frames: list[dict]         # [{symbol, file, line}], max 5
    blame_symbol: str | None
    blame_file: str | None
    blame_line: int | None

    @property
    def kind(self) -> str:
        return "ANR" if self.error_type == "ANR" else "Crash"

    @property
    def title(self) -> str:
        name = (self.exception_type or self.kind).split(".")[-1]
        return name or self.kind

    @property
    def location(self) -> str | None:
        if self.blame_file:
            return f"{self.blame_file}:{self.blame_line}" if self.blame_line else self.blame_file
        if self.blame_symbol:
            return self.blame_symbol
        for f in self.frames:
            if f.get("file"):
                return f"{f['file']}:{f.get('line')}" if f.get("line") else f["file"]
        return None


@dataclass
class Period:
    crashes: int = 0
    anrs: int = 0
    users_affected: int = 0
    active_users: int | None = None
    crashed_users: int | None = None
    reported_crash_free_pct: float | None = None   # straight from Crashlytics, when it gives one

    @property
    def crash_free_pct(self) -> float | None:
        if self.reported_crash_free_pct is not None:
            return round(self.reported_crash_free_pct, 2)
        if not self.active_users:
            return None
        return round(100.0 * (1 - (self.crashed_users or 0) / self.active_users), 2)


@dataclass
class Digest:
    current: Period
    previous: Period
    issues: list[Issue] = field(default_factory=list)
    sessions_enabled: bool = False

    @property
    def new_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.is_new]

    @property
    def returning_issues(self) -> list[Issue]:
        return [i for i in self.issues if i.is_returning]

    def top_issues(self, n: int) -> list[Issue]:
        return self.issues[:n]

    @property
    def has_any_events(self) -> bool:
        return bool(self.current.crashes or self.current.anrs)

    @property
    def looks_like_no_data(self) -> bool:
        """True when we can't tell 'zero crashes' from 'export broken'.

        With sessions export we can: sessions > 0 and crashes == 0 is a genuinely clean day.
        """
        if self.has_any_events:
            return False
        if self.sessions_enabled:
            return not self.current.active_users
        return not (self.previous.crashes or self.previous.anrs)


def _pairs(values) -> list[tuple[str, int]]:
    out = []
    for v in values or []:
        label = (v.get("value") or "unknown").strip() or "unknown"
        out.append((label, int(v.get("count") or 0)))
    return out


def build_digest(headline: list[dict], crash_free: list[dict] | None, issues: list[dict]) -> Digest:
    periods = {"current": Period(), "previous": Period()}
    for row in headline:
        p = periods.get(row["period"])
        if p:
            p.crashes = int(row.get("crashes") or 0)
            p.anrs = int(row.get("anrs") or 0)
            p.users_affected = int(row.get("users_affected") or 0)
    for row in crash_free or []:
        p = periods.get(row["period"])
        if p:
            p.active_users = int(row.get("active_users") or 0)
            p.crashed_users = int(row.get("crashed_users") or 0)
            if row.get("crash_free_users_pct") is not None:
                p.reported_crash_free_pct = float(row["crash_free_users_pct"])

    parsed = [
        Issue(
            issue_id=str(r["issue_id"]),
            error_type=r.get("error_type") or "FATAL",
            events=int(r.get("events") or 0),
            users=int(r.get("users") or 0),
            is_new=bool(r.get("is_new")),
            is_returning=bool(r.get("is_returning")),
            top_versions=_pairs(r.get("top_versions")),
            top_devices=_pairs(r.get("top_devices")),
            top_os=_pairs(r.get("top_os")),
            exception_type=r.get("exception_type"),
            frames=[
                {"symbol": f.get("symbol"), "file": f.get("file"), "line": f.get("line")}
                for f in (r.get("frames") or [])
            ][:5],
            blame_symbol=r.get("blame_symbol"),
            blame_file=r.get("blame_file"),
            blame_line=r.get("blame_line"),
        )
        for r in issues
    ]
    parsed.sort(key=lambda i: (i.users, i.events), reverse=True)
    return Digest(
        current=periods["current"],
        previous=periods["previous"],
        issues=parsed,
        sessions_enabled=crash_free is not None,
    )
