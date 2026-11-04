"""Reporting queries over the tracking database."""
from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict

from sqlalchemy import func, select

from .db import IN_FLIGHT_STATES, Database
from .models import Event, FastqFile, Library, PipelineJob, QCMetric, Run, Sample

NO_PROJECT = "(none)"
_MAD_SCALE = 0.6745  # makes MAD comparable to a standard deviation for normal data


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


class Reporter:
    def __init__(self, db: Database) -> None:
        self.db = db

    def status_counts(self, since: dt.datetime | None = None) -> dict[str, int]:
        """Runs per status; with since, only runs first detected at or after that time."""
        stmt = select(Run.status, func.count(Run.id)).group_by(Run.status)
        if since is not None:
            stmt = stmt.where(Run.detected_at >= since)
        with self.db.session() as s:
            return {status: n for status, n in s.execute(stmt)}

    def yield_by_project(self, since: dt.datetime | None = None) -> list[dict]:
        """Runs, samples, FASTQ files and bytes per project (samples without files included)."""
        stmt = (
            select(
                Sample.project,
                func.count(func.distinct(Sample.run_id)),
                func.count(func.distinct(Sample.id)),
                func.count(FastqFile.id),
                func.coalesce(func.sum(FastqFile.size_bytes), 0),
            )
            .select_from(Sample)
            .join(Run, Run.id == Sample.run_id)
            .outerjoin(Library, Library.sample_pk == Sample.id)
            .outerjoin(FastqFile, FastqFile.library_id == Library.id)
            .group_by(Sample.project)
            .order_by(func.coalesce(func.sum(FastqFile.size_bytes), 0).desc(), Sample.project)
        )
        if since is not None:
            stmt = stmt.where(Run.detected_at >= since)
        with self.db.session() as s:
            return [
                {"project": project or NO_PROJECT, "runs": runs, "samples": samples,
                 "fastq_files": files, "total_bytes": int(total)}
                for project, runs, samples, files, total in s.execute(stmt)
            ]

    def failed_runs(self, since: dt.datetime | None = None) -> list[dict]:
        """Failed runs with their most recent error message, newest first.
        With since, only runs whose last update is at or after that time."""
        last_error = (
            select(func.max(Event.id))
            .where(Event.run_id == Run.id, Event.level == "error")
            .correlate(Run)
            .scalar_subquery()
        )
        stmt = (
            select(Run.run_id, Run.platform, Run.updated_at, Event.message)
            .outerjoin(Event, Event.id == last_error)
            .where(Run.status == "failed")
            .order_by(Run.updated_at.desc(), Run.run_id)
        )
        if since is not None:
            stmt = stmt.where(Run.updated_at >= since)
        with self.db.session() as s:
            return [{"run_id": rid, "platform": plat, "updated_at": _iso(upd), "error": msg}
                    for rid, plat, upd, msg in s.execute(stmt)]

    def stale_runs(self, older_than: dt.datetime) -> list[dict]:
        """Runs still in an in-flight state whose last update is before older_than.
        While a watcher is alive these are runs that look stuck."""
        stmt = (
            select(Run.run_id, Run.status, Run.updated_at)
            .where(Run.status.in_(IN_FLIGHT_STATES), Run.updated_at < older_than)
            .order_by(Run.updated_at, Run.run_id)
        )
        with self.db.session() as s:
            return [{"run_id": rid, "status": st, "updated_at": _iso(upd)}
                    for rid, st, upd in s.execute(stmt)]

    def event_counts(self, since: dt.datetime) -> dict[str, int]:
        """Number of events per level since the given time."""
        with self.db.session() as s:
            return dict(s.execute(
                select(Event.level, func.count(Event.id))
                .where(Event.created_at >= since).group_by(Event.level)).all())

    def qc_table(self, run_id: str) -> list[dict]:
        """One row per sample with every stored metric as a column."""
        with self.db.session() as s:
            rows = s.execute(
                select(QCMetric.sample_id, QCMetric.name, QCMetric.value)
                .join(Run, Run.id == QCMetric.run_id)
                .where(Run.run_id == run_id)
                .order_by(QCMetric.sample_id, QCMetric.name)
            ).all()
        table: dict[str, dict] = defaultdict(dict)
        for sample, name, value in rows:
            table[sample or NO_PROJECT][name] = value
        return [{"sample": sample, **metrics} for sample, metrics in sorted(table.items())]

    def qc_outliers(self, metric: str, *, run_id: str | None = None, source: str | None = None,
                    threshold: float = 3.5, min_samples: int = 5) -> list[dict]:
        """Samples whose metric is an outlier by the modified z-score (median/MAD).

        Median and MAD are used instead of mean and standard deviation because they
        stay meaningful with few samples and are not distorted by the outliers
        themselves. 3.5 is the usual cut-off. Nothing is reported when there are fewer
        than min_samples values or when the MAD is zero.
        """
        stmt = (
            select(Run.run_id, QCMetric.sample_id, QCMetric.value)
            .join(Run, Run.id == QCMetric.run_id)
            .where(QCMetric.name == metric)
        )
        if run_id is not None:
            stmt = stmt.where(Run.run_id == run_id)
        if source is not None:
            stmt = stmt.where(QCMetric.source == source)
        with self.db.session() as s:
            rows = s.execute(stmt).all()
        if len(rows) < min_samples:
            return []
        values = [v for _, _, v in rows]
        median = statistics.median(values)
        mad = statistics.median(abs(v - median) for v in values)
        if mad == 0:
            return []
        out = []
        for rid, sample, value in rows:
            z = _MAD_SCALE * (value - median) / mad
            if abs(z) >= threshold:
                out.append({"run_id": rid, "sample": sample, "metric": metric,
                            "value": value, "modified_z": round(z, 2),
                            "median": median})
        return sorted(out, key=lambda r: -abs(r["modified_z"]))

    def run_events(self, run_id: str, level: str | None = None) -> list[dict]:
        stmt = (
            select(Event.created_at, Event.level, Event.message)
            .join(Run, Run.id == Event.run_id)
            .where(Run.run_id == run_id)
            .order_by(Event.id)
        )
        if level is not None:
            stmt = stmt.where(Event.level == level)
        with self.db.session() as s:
            return [{"time": _iso(t), "level": lvl, "message": msg} for t, lvl, msg in s.execute(stmt)]

    def run_report(self, run_id: str) -> dict:
        """Summary, job list and event counts for one run."""
        summary = self.db.run_summary(run_id)
        with self.db.session() as s:
            jobs = [{"name": n, "status": st, "exit_code": code}
                    for n, st, code in s.execute(
                        select(PipelineJob.name, PipelineJob.status, PipelineJob.exit_code)
                        .join(Run, Run.id == PipelineJob.run_id)
                        .where(Run.run_id == run_id).order_by(PipelineJob.id))]
            levels = dict(s.execute(
                select(Event.level, func.count(Event.id))
                .join(Run, Run.id == Event.run_id)
                .where(Run.run_id == run_id).group_by(Event.level)).all())
            n_metrics = s.scalar(
                select(func.count(QCMetric.id)).join(Run, Run.id == QCMetric.run_id)
                .where(Run.run_id == run_id))
        return {**summary, "jobs": jobs, "events_by_level": levels, "qc_metrics": n_metrics}


def format_table(rows: list[dict]) -> str:
    """Fixed-width text table; columns are the union of keys in first-seen order."""
    if not rows:
        return "(no rows)"
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)

    def cell(value) -> str:
        if value is None:
            return ""
        return f"{value:.4g}" if isinstance(value, float) else str(value)

    body = [[cell(r.get(c)) for c in columns] for r in rows]
    widths = [max(len(c), *(len(b[i]) for b in body)) for i, c in enumerate(columns)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)),
             "  ".join("-" * w for w in widths)]
    lines += ["  ".join(v.ljust(w) for v, w in zip(b, widths, strict=True)) for b in body]
    return "\n".join(lines)
