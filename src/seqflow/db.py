"""Database access layer. One class, one session factory, explicit transactions."""
from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .exceptions import StateError
from .models import Base, Event, FastqFile, Library, PipelineJob, QCMetric, Run, Sample

RUN_STATES = ("detected", "demultiplexing", "demultiplexed", "verified", "pipeline_running", "done", "failed")

# States a run only holds while a process is working on it. Finding a run in one of
# them when no process is running means the previous process died mid-run.
IN_FLIGHT_STATES = ("demultiplexing", "demultiplexed", "verified", "pipeline_running")


class Database:
    def __init__(self, url: str = "sqlite:///seqflow.db", echo: bool = False) -> None:
        self.url = url
        self.engine: Engine = create_engine(url, echo=echo, future=True)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """Create tables straight from the models (tests, throw-away databases).
        Real databases should use upgrade() so the schema history is tracked."""
        Base.metadata.create_all(self.engine)

    # ---- schema migrations ---------------------------------------------
    def _alembic_config(self) -> Config:
        cfg = Config()
        cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        return cfg

    def upgrade(self, revision: str = "head") -> None:
        cfg = self._alembic_config()
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, revision)

    def downgrade(self, revision: str) -> None:
        cfg = self._alembic_config()
        with self.engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, revision)

    def schema_revision(self) -> str | None:
        """Current migration revision of the connected database (None if unversioned)."""
        with self.engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # ---- writes -------------------------------------------------------
    def get_or_create_run(self, run_id: str, platform: str, path: str) -> Run:
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                run = Run(run_id=run_id, platform=platform, path=path)
                s.add(run)
                s.flush()
            return run

    def set_status(self, run_id: str, status: str) -> None:
        if status not in RUN_STATES:
            raise ValueError(f"unknown run status: {status}")
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            if run.status == status:
                return  # no state change, no event: polling must not flood the log
            run.status = status
            s.add(Event(run_id=run.id, level="info", message=f"status -> {status}"))

    def requeue(self, run_id: str, *, force: bool = False) -> str:
        """Send a failed run back to "detected" so the watcher processes it again.

        Done runs need force=True. Runs that are in flight are refused: restarting the
        watcher recovers those, and requeueing a run that is being processed would
        start it twice. Returns the previous status.
        """
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            allowed = {"failed", "done"} if force else {"failed"}
            if run.status not in allowed:
                hint = " (use --force for finished runs)" if run.status == "done" else ""
                raise StateError(f"run {run_id} is {run.status}; only failed runs can be requeued{hint}")
            previous = run.status
            run.status = "detected"
            s.add(Event(run_id=run.id, level="info", message=f"requeued from {previous}"))
            return previous

    def log_event(self, run_id: str, level: str, message: str, *,
                  if_changed: bool = False) -> bool:
        """Append an event. With if_changed, skip it when the newest event of the run
        has the same level and message. Returns True when an event was written."""
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            if if_changed:
                last = s.execute(
                    select(Event.level, Event.message).where(Event.run_id == run.id)
                    .order_by(Event.id.desc()).limit(1)
                ).first()
                if last is not None and tuple(last) == (level, message):
                    return False
            s.add(Event(run_id=run.id, level=level, message=message))
            return True

    def add_sample(self, run_id: str, sample_id: str, project: str | None,
                   index1: str | None, index2: str | None, lane: int | None) -> bool:
        """Register a sample of a run. Returns False when it already exists, so
        processing a run a second time is safe."""
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            exists = s.scalar(select(Sample.id).where(Sample.run_id == run.id,
                                                      Sample.sample_id == sample_id))
            if exists is not None:
                return False
            smp = Sample(run_id=run.id, sample_id=sample_id, project=project)
            smp.libraries.append(Library(index1=index1, index2=index2, lane=lane))
            s.add(smp)
            return True

    def add_fastq(self, run_id: str, sample_id: str, path: str, read: str,
                  size_bytes: int, md5: str, *, verified: bool = False) -> None:
        """Record a FASTQ file. Creates the sample if the sample sheet did not list it
        (ONT runs, Undetermined reads) and updates the row when the path is known."""
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            smp = s.scalar(select(Sample).where(Sample.run_id == run.id,
                                                Sample.sample_id == sample_id))
            if smp is None:
                smp = Sample(run_id=run.id, sample_id=sample_id)
                smp.libraries.append(Library())
                s.add(smp)
                s.flush()
            stamp = dt.datetime.now(dt.timezone.utc) if verified else None
            known = s.scalar(select(FastqFile).where(FastqFile.path == path))
            if known is not None:
                known.read, known.size_bytes, known.md5 = read, size_bytes, md5
                if stamp:
                    known.verified_at = stamp
                return
            if not smp.libraries:
                smp.libraries.append(Library())
            smp.libraries[0].fastq_files.append(FastqFile(
                path=path, read=read, size_bytes=size_bytes, md5=md5, verified_at=stamp))

    def add_qc(self, run_id: str, sample_id: str | None, name: str, value: float,
               source: str = "manual") -> None:
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            s.add(QCMetric(run_id=run.id, sample_id=sample_id, name=name, value=value,
                           source=source))

    def replace_qc(self, run_id: str, source: str,
                   rows: Iterable[tuple[str | None, str, float]]) -> int:
        """Replace all metrics of one source for a run (idempotent re-ingest).

        Repeated (sample, name) pairs in rows collapse to the last value. Returns the
        number of metric rows stored.
        """
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            s.execute(delete(QCMetric).where(QCMetric.run_id == run.id,
                                             QCMetric.source == source))
            unique = {(sid, name): value for sid, name, value in rows}
            s.add_all(
                QCMetric(run_id=run.id, sample_id=sid, name=name, value=value, source=source)
                for (sid, name), value in unique.items()
            )
            return len(unique)

    def add_job(self, run_id: str, name: str, command: str) -> int:
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            job = PipelineJob(run_id=run.id, name=name, command=command)
            s.add(job)
            s.flush()
            return job.id

    # ---- reads --------------------------------------------------------
    def runs_by_status(self, status: str) -> list[Run]:
        with self.session() as s:
            return list(s.scalars(select(Run).where(Run.status == status)))

    def run_ids_with_status(self, statuses: Iterable[str]) -> set[str]:
        """Run ids in any of the given states, in a single query."""
        with self.session() as s:
            return set(s.scalars(select(Run.run_id).where(Run.status.in_(list(statuses)))))

    def run_summary(self, run_id: str) -> dict:
        """Sample count, FASTQ count and total bytes of a run (joins runs/samples/libraries/fastq)."""
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            n_samples = s.scalar(select(func.count(Sample.id)).where(Sample.run_id == run.id))
            n_fastq, total = s.execute(
                select(func.count(FastqFile.id), func.coalesce(func.sum(FastqFile.size_bytes), 0))
                .join(Library).join(Sample).where(Sample.run_id == run.id)
            ).one()
            return {"run_id": run_id, "status": run.status, "samples": n_samples,
                    "fastq_files": n_fastq, "total_bytes": int(total)}
