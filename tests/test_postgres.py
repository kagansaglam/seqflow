"""Tests that need a real PostgreSQL server.

Set SEQFLOW_TEST_PG_URL, e.g. postgresql+psycopg2://seqflow:seqflow@localhost:5432/seqflow.
Every test works in its own throw-away schema, so existing data is never touched.
"""
import os
import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from seqflow.db import Database
from seqflow.models import Base
from seqflow.reports import Reporter

PG_URL = os.environ.get("SEQFLOW_TEST_PG_URL")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not PG_URL, reason="SEQFLOW_TEST_PG_URL not set"),
]

GB = 1_000_000_000


@pytest.fixture
def pg():
    schema = f"seqflow_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    sep = "&" if "?" in PG_URL else "?"
    db = Database(f"{PG_URL}{sep}options=-csearch_path%3D{schema}")
    yield db
    db.engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_upgrade_and_models_match(pg):
    pg.upgrade()
    assert pg.schema_revision() == "0002"
    with pg.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_file_sizes_above_2_gib_are_stored(pg):
    pg.upgrade()
    col = next(c for c in inspect(pg.engine).get_columns("fastq_files") if c["name"] == "size_bytes")
    assert "BIGINT" in str(col["type"]).upper()
    pg.get_or_create_run("R1", "illumina", "/x")
    pg.add_sample("R1", "S1", "p", "AAAA", "CCCC", 1)
    pg.add_fastq("R1", "S1", "/o/big.fastq.gz", "R1", 12 * GB, "a" * 32)
    assert pg.run_summary("R1")["total_bytes"] == 12 * GB


def test_0001_data_survives_upgrade_to_0002(pg):
    pg.upgrade("0001")
    with pg.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO runs (run_id, platform, path, status, detected_at, updated_at) "
            "VALUES ('R1', 'illumina', '/x', 'done', now(), now())"))
        conn.execute(text("INSERT INTO qc_metrics (run_id, sample_id, name, value) "
                          "VALUES (1, 'S1', 'gc', 48.0)"))
    pg.upgrade()
    with pg.engine.connect() as conn:
        assert conn.execute(text("SELECT source FROM qc_metrics")).scalar_one() == "manual"


def test_qc_uniqueness_is_enforced(pg):
    pg.upgrade()
    pg.get_or_create_run("R1", "illumina", "/x")
    pg.add_qc("R1", "S1", "gc", 48.0, source="fastqc")
    with pytest.raises(IntegrityError):
        pg.add_qc("R1", "S1", "gc", 49.0, source="fastqc")
    pg.add_qc("R1", "S1", "gc", 49.0, source="multiqc")  # other source is fine


def test_reports_on_postgres(pg):
    pg.upgrade()
    pg.get_or_create_run("R1", "illumina", "/x")
    pg.add_sample("R1", "S1", "projA", "AAAA", "CCCC", 1)
    pg.add_sample("R1", "S2", None, None, None, None)
    pg.add_fastq("R1", "S1", "/o/S1.fastq.gz", "R1", 3 * GB, "a" * 32)
    pg.log_event("R1", "error", "first")
    pg.log_event("R1", "error", "second")
    pg.set_status("R1", "failed")
    for i, v in enumerate([48, 49, 47, 48, 50, 48, 49, 30]):
        pg.add_qc("R1", f"S{i}", "R1.percent_gc", v, source="fastqc")

    rep = Reporter(pg)
    rows = {r["project"]: r for r in rep.yield_by_project()}
    assert rows["projA"]["total_bytes"] == 3 * GB and rows["(none)"]["samples"] == 1
    assert rep.failed_runs()[0]["error"] == "second"
    assert [o["sample"] for o in rep.qc_outliers("R1.percent_gc")] == ["S7"]
    assert rep.status_counts() == {"failed": 1}
