import datetime as dt

import pytest
from sqlalchemy import select

from seqflow.digest import build_digest
from seqflow.models import Run
from seqflow.reports import Reporter

GB = 1_000_000_000


def age(db, run_id, hours):
    """Pretend the run was last updated `hours` ago (and first seen then)."""
    when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    with db.session() as s:
        run = s.scalar(select(Run).where(Run.run_id == run_id))
        run.updated_at = when
        run.detected_at = when


def add_run(db, run_id, status, project="projA", size=GB):
    db.get_or_create_run(run_id, "illumina", f"/data/{run_id}")
    db.add_sample(run_id, "S1", project, "AAAA", "CCCC", 1)
    db.add_fastq(run_id, "S1", f"/o/{run_id}.fastq.gz", "R1", size, "a" * 32)
    db.set_status(run_id, status)


def test_quiet_day_is_info(db):
    add_run(db, "R1", "done")
    level, subject, body = build_digest(db)
    assert level == "info" and subject.startswith("digest: 1 new run(s), 0 failed, 0 stuck")
    assert "projA: 1 samples, 1 files, 1.00 GB" in body
    assert "Failed runs" not in body


def test_failed_run_inside_the_window_needs_attention(db):
    add_run(db, "R1", "detected")
    db.log_event("R1", "error", "bcl-convert exited 3")
    db.set_status("R1", "failed")
    level, subject, body = build_digest(db)
    assert level == "error" and "attention needed" in subject
    assert "R1 (illumina): bcl-convert exited 3" in body
    assert "1 errors" in body


def test_old_failures_and_old_runs_are_outside_the_window(db):
    add_run(db, "OLD", "failed")
    age(db, "OLD", 72)
    level, _, body = build_digest(db, hours=24)
    assert level == "info" and "New runs: none" in body and "OLD" not in body
    assert "OLD" in build_digest(db, hours=100)[2]


def test_stuck_run_is_flagged_but_a_fresh_one_is_not(db):
    add_run(db, "STUCK", "demultiplexing")
    add_run(db, "BUSY", "demultiplexing")
    age(db, "STUCK", 20)
    level, _, body = build_digest(db, hours=48, stale_hours=12)
    assert level == "error"
    assert "STUCK stuck in demultiplexing" in body and "BUSY" not in body.split("no progress")[1]


def test_stale_runs_only_lists_in_flight_states(db):
    for rid, status in [("A", "demultiplexing"), ("B", "done"), ("C", "failed"), ("D", "detected")]:
        add_run(db, rid, status)
        age(db, rid, 30)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=12)
    assert [r["run_id"] for r in Reporter(db).stale_runs(cutoff)] == ["A"]


def test_status_counts_since(db):
    add_run(db, "NEW", "done")
    add_run(db, "OLD", "done")
    age(db, "OLD", 100)
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
    assert Reporter(db).status_counts(since) == {"done": 1}
    assert Reporter(db).status_counts() == {"done": 2}


def test_event_counts_since(db):
    add_run(db, "R1", "detected")
    db.log_event("R1", "warning", "w1")
    db.log_event("R1", "warning", "w2")
    db.log_event("R1", "error", "e")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    counts = Reporter(db).event_counts(since)
    assert counts["warning"] == 2 and counts["error"] == 1
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
    assert Reporter(db).event_counts(future) == {}


@pytest.mark.parametrize("hours", [1, 24, 168])
def test_header_mentions_the_window(db, hours):
    assert f"last {hours} h" in build_digest(db, hours=hours)[2]
