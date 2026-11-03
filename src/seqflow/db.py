"""Database access layer. One class, one session factory, explicit transactions."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, Event, FastqFile, Library, PipelineJob, QCMetric, Run, Sample

RUN_STATES = ("detected", "demultiplexing", "demultiplexed", "verified", "pipeline_running", "done", "failed")


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
            run.status = status
            s.add(Event(run_id=run.id, level="info", message=f"status -> {status}"))

    def log_event(self, run_id: str, level: str, message: str) -> None:
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            if run is None:
                raise KeyError(run_id)
            s.add(Event(run_id=run.id, level=level, message=message))

    def add_sample(self, run_id: str, sample_id: str, project: str | None,
                   index1: str | None, index2: str | None, lane: int | None) -> None:
        with self.session() as s:
            run = s.scalar(select(Run).where(Run.run_id == run_id))
            smp = Sample(run_id=run.id, sample_id=sample_id, project=project)
            smp.libraries.append(Library(index1=index1, index2=index2, lane=lane))
            s.add(smp)

    def add_fastq(self, run_id: str, sample_id: str, path: str, read: str,
                  size_bytes: int, md5: str) -> None:
        with self.session() as s:
            smp = s.scalar(
                select(Sample).join(Run).where(Run.run_id == run_id, Sample.sample_id == sample_id)
            )
            lib = smp.libraries[0]
            lib.fastq_files.append(FastqFile(path=path, read=read, size_bytes=size_bytes, md5=md5))

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
