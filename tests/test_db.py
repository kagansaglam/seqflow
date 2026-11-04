import pytest


def test_run_lifecycle_and_summary(db):
    db.get_or_create_run("R1", "illumina", "/data/R1")
    db.add_sample("R1", "S1", "p", "ACGT", "TTTT", 1)
    db.add_fastq("R1", "S1", "/o/S1_R1.fastq.gz", "R1", 100, "a" * 32)
    db.add_fastq("R1", "S1", "/o/S1_R2.fastq.gz", "R2", 200, "b" * 32)
    db.set_status("R1", "demultiplexed")
    s = db.run_summary("R1")
    assert s == {"run_id": "R1", "status": "demultiplexed", "samples": 1,
                 "fastq_files": 2, "total_bytes": 300}


def test_get_or_create_is_idempotent(db):
    a = db.get_or_create_run("R1", "illumina", "/x")
    b = db.get_or_create_run("R1", "illumina", "/x")
    assert a.id == b.id


def test_invalid_status_rejected(db):
    db.get_or_create_run("R1", "illumina", "/x")
    with pytest.raises(ValueError):
        db.set_status("R1", "bogus")


def test_unknown_run(db):
    with pytest.raises(KeyError):
        db.set_status("nope", "done")


def test_runs_by_status(db):
    db.get_or_create_run("R1", "illumina", "/x")
    db.get_or_create_run("R2", "illumina", "/y")
    db.set_status("R2", "failed")
    assert [r.run_id for r in db.runs_by_status("failed")] == ["R2"]


def test_add_sample_is_idempotent(db):
    db.get_or_create_run("R1", "illumina", "/x")
    assert db.add_sample("R1", "S1", "p", "AAAA", "CCCC", 1) is True
    assert db.add_sample("R1", "S1", "p", "AAAA", "CCCC", 1) is False
    assert db.run_summary("R1")["samples"] == 1
    with pytest.raises(KeyError):
        db.add_sample("nope", "S1", None, None, None, None)


def test_set_status_without_change_writes_no_event(db):
    db.get_or_create_run("R1", "illumina", "/x")
    db.set_status("R1", "detected")
    db.set_status("R1", "demultiplexing")
    db.set_status("R1", "demultiplexing")
    from seqflow.reports import Reporter
    assert [e["message"] for e in Reporter(db).run_events("R1")] == ["status -> demultiplexing"]


def test_log_event_if_changed_only_skips_identical_latest(db):
    db.get_or_create_run("R1", "illumina", "/x")
    assert db.log_event("R1", "warning", "w", if_changed=True) is True
    assert db.log_event("R1", "warning", "w", if_changed=True) is False
    assert db.log_event("R1", "error", "e") is True
    assert db.log_event("R1", "warning", "w", if_changed=True) is True  # latest differs now
    assert db.log_event("R1", "warning", "w") is True  # without the flag it always writes


def test_add_fastq_creates_missing_sample_and_upserts_by_path(db):
    db.get_or_create_run("R1", "ont", "/x")
    db.add_fastq("R1", "barcode01", "/o/b1.fastq.gz", "SE", 10, "a" * 32)
    db.add_fastq("R1", "barcode01", "/o/b1.fastq.gz", "SE", 20, "b" * 32, verified=True)
    s = db.run_summary("R1")
    assert (s["samples"], s["fastq_files"], s["total_bytes"]) == (1, 1, 20)
    with pytest.raises(KeyError):
        db.add_fastq("nope", "x", "/o/y.fastq.gz", "SE", 1, "c" * 32)


def test_run_ids_with_status(db):
    for rid, st in [("A", "done"), ("B", "failed"), ("C", "detected")]:
        db.get_or_create_run(rid, "illumina", "/x")
        db.set_status(rid, st)
    assert db.run_ids_with_status(["done", "failed"]) == {"A", "B"}
    assert db.run_ids_with_status([]) == set()
