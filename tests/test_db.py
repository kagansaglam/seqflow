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
