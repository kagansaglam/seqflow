"""Database schema (SQLAlchemy 2.0 ORM). Works on PostgreSQL and SQLite.

Tables: runs, samples, libraries, fastq_files, qc_metrics, pipeline_jobs, events.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32))  # illumina | ont | pacbio
    path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="detected", index=True)
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    samples: Mapped[list[Sample]] = relationship(back_populates="run", cascade="all, delete-orphan")
    jobs: Mapped[list[PipelineJob]] = relationship(back_populates="run", cascade="all, delete-orphan")
    events: Mapped[list[Event]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Sample(Base):
    __tablename__ = "samples"
    __table_args__ = (UniqueConstraint("run_id", "sample_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    sample_id: Mapped[str] = mapped_column(String(128))
    project: Mapped[str | None] = mapped_column(String(128), index=True)

    run: Mapped[Run] = relationship(back_populates="samples")
    libraries: Mapped[list[Library]] = relationship(back_populates="sample", cascade="all, delete-orphan")


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_pk: Mapped[int] = mapped_column(ForeignKey("samples.id"), index=True)
    index1: Mapped[str | None] = mapped_column(String(32))
    index2: Mapped[str | None] = mapped_column(String(32))
    lane: Mapped[int | None] = mapped_column(Integer)

    sample: Mapped[Sample] = relationship(back_populates="libraries")
    fastq_files: Mapped[list[FastqFile]] = relationship(back_populates="library", cascade="all, delete-orphan")


class FastqFile(Base):
    __tablename__ = "fastq_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    library_id: Mapped[int] = mapped_column(ForeignKey("libraries.id"), index=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    read: Mapped[str] = mapped_column(String(8))  # R1 | R2 | SE
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)  # FASTQ files exceed 2 GiB
    md5: Mapped[str | None] = mapped_column(String(32))
    verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    library: Mapped[Library] = relationship(back_populates="fastq_files")


class QCMetric(Base):
    __tablename__ = "qc_metrics"
    __table_args__ = (
        UniqueConstraint("run_id", "sample_id", "name", "source", name="uq_qc_metric"),
        Index("ix_qc_metrics_run_name", "run_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    sample_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    command: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)

    run: Mapped[Run] = relationship(back_populates="jobs")


class Event(Base):
    """Audit log: every state change, warning and error for a run."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    level: Mapped[str] = mapped_column(String(16))  # info | warning | error
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    run: Mapped[Run] = relationship(back_populates="events")
