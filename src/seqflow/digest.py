"""Daily digest: what happened in the last N hours and what needs attention."""
from __future__ import annotations

import datetime as dt

from .db import Database
from .reports import Reporter

DEFAULT_HOURS = 24.0
DEFAULT_STALE_HOURS = 12.0


def _gb(n_bytes: int) -> str:
    return f"{n_bytes / 1e9:.2f} GB"


def build_digest(db: Database, *, hours: float = DEFAULT_HOURS,
                 stale_hours: float = DEFAULT_STALE_HOURS,
                 now: dt.datetime | None = None) -> tuple[str, str, str]:
    """Return (level, subject, body). Level is "error" when something needs attention
    (failed or stuck runs) and "info" otherwise."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(hours=hours)
    rep = Reporter(db)

    new_runs = rep.status_counts(since)
    failed = rep.failed_runs(since)
    stuck = rep.stale_runs(now - dt.timedelta(hours=stale_hours))
    events = rep.event_counts(since)
    yields = rep.yield_by_project(since)

    lines = [f"seqflow digest for the last {hours:g} h (until {now:%Y-%m-%d %H:%M} UTC)", ""]
    total_new = sum(new_runs.values())
    if total_new:
        parts = ", ".join(f"{n} {status}" for status, n in sorted(new_runs.items()))
        lines.append(f"New runs: {total_new} ({parts})")
    else:
        lines.append("New runs: none")
    lines.append(f"Events: {events.get('error', 0)} errors, {events.get('warning', 0)} warnings")

    if yields:
        lines += ["", "Yield by project:"]
        lines += [f"  {y['project']}: {y['samples']} samples, {y['fastq_files']} files, "
                  f"{_gb(y['total_bytes'])}" for y in yields]

    if failed:
        lines += ["", f"Failed runs ({len(failed)}):"]
        lines += [f"  {f['run_id']} ({f['platform']}): {f['error'] or 'no error message'}"
                  for f in failed]
    if stuck:
        lines += ["", f"Runs with no progress for over {stale_hours:g} h ({len(stuck)}):"]
        lines += [f"  {s['run_id']} stuck in {s['status']} since {s['updated_at']}" for s in stuck]

    attention = bool(failed or stuck)
    subject = ("digest: attention needed - " if attention else "digest: ") + (
        f"{total_new} new run(s), {len(failed)} failed, {len(stuck)} stuck")
    return ("error" if attention else "info"), subject, "\n".join(lines)
