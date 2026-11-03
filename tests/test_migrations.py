import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from seqflow.db import Database
from seqflow.models import Base


@pytest.fixture
def fresh(tmp_path):
    d = Database(f"sqlite:///{tmp_path / 'm.db'}")
    yield d
    d.engine.dispose()


def test_upgrade_creates_every_table_and_records_revision(fresh):
    assert fresh.schema_revision() is None
    fresh.upgrade()
    tables = set(inspect(fresh.engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert fresh.schema_revision() == "0002"


def test_migrations_match_the_models(fresh):
    """If a model changes without a new migration, this fails."""
    fresh.upgrade()
    with fresh.engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []


def test_upgrade_is_idempotent(fresh):
    fresh.upgrade()
    fresh.upgrade()
    assert fresh.schema_revision() == "0002"


def test_step_by_step_and_downgrade(fresh):
    fresh.upgrade("0001")
    assert fresh.schema_revision() == "0001"
    cols = {c["name"] for c in inspect(fresh.engine).get_columns("qc_metrics")}
    assert "source" not in cols
    fresh.upgrade("0002")
    cols = {c["name"] for c in inspect(fresh.engine).get_columns("qc_metrics")}
    assert "source" in cols
    fresh.downgrade("0001")
    assert fresh.schema_revision() == "0001"


def test_existing_rows_survive_the_upgrade(fresh):
    """Data written under 0001 gets the default source after 0002."""
    fresh.upgrade("0001")
    with fresh.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO runs (run_id, platform, path, status, detected_at, updated_at) "
            "VALUES ('R1', 'illumina', '/x', 'done', '2026-01-01', '2026-01-01')"
        )
        conn.exec_driver_sql(
            "INSERT INTO qc_metrics (run_id, sample_id, name, value) VALUES (1, 'S1', 'gc', 48.0)"
        )
    fresh.upgrade()
    with fresh.engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT source, value FROM qc_metrics").one()
    assert tuple(row) == ("manual", 48.0)
