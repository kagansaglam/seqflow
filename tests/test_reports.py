import datetime as dt

import pytest

from seqflow.reports import NO_PROJECT, Reporter, format_table

GB = 1_000_000_000


@pytest.fixture
def seeded(db):
    """Two runs: R1 has three samples in two projects, R2 one sample plus one without project."""
    db.get_or_create_run("R1", "illumina", "/data/R1")
    db.add_sample("R1", "S1", "projA", "AAAA", "CCCC", 1)
    db.add_sample("R1", "S2", "projA", "GGGG", "TTTT", 1)  # no FASTQ files yet
    db.add_sample("R1", "S3", "projB", "ACAC", "GTGT", 1)
    db.add_fastq("R1", "S1", "/o/S1_R1.fastq.gz", "R1", 100, "a" * 32)
    db.add_fastq("R1", "S1", "/o/S1_R2.fastq.gz", "R2", 200, "b" * 32)
    db.add_fastq("R1", "S3", "/o/S3_R1.fastq.gz", "R1", 5 * GB, "c" * 32)
    db.get_or_create_run("R2", "ont", "/data/R2")
    db.add_sample("R2", "S4", "projA", None, None, None)
    db.add_sample("R2", "S5", None, None, None, None)
    db.add_fastq("R2", "S4", "/o/S4.fastq.gz", "SE", 50, "d" * 32)
    return db


def test_status_counts(seeded):
    seeded.set_status("R2", "failed")
    assert Reporter(seeded).status_counts() == {"detected": 1, "failed": 1}


def test_yield_by_project(seeded):
    rows = Reporter(seeded).yield_by_project()
    by = {r["project"]: r for r in rows}
    assert by["projA"] == {"project": "projA", "runs": 2, "samples": 3, "fastq_files": 3,
                           "total_bytes": 350}
    assert by["projB"]["total_bytes"] == 5 * GB and by["projB"]["runs"] == 1
    assert by[NO_PROJECT]["fastq_files"] == 0  # sample without files still counted
    assert [r["project"] for r in rows] == ["projB", "projA", NO_PROJECT]  # biggest first


def test_yield_since_filters_runs(seeded):
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    assert Reporter(seeded).yield_by_project(since=future) == []
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    assert len(Reporter(seeded).yield_by_project(since=past)) == 3


def test_failed_runs_report_latest_error(seeded):
    seeded.log_event("R1", "error", "first problem")
    seeded.log_event("R1", "warning", "just a warning")
    seeded.log_event("R1", "error", "second problem")
    seeded.set_status("R1", "failed")
    seeded.set_status("R2", "failed")  # failed without any error event
    rows = {r["run_id"]: r for r in Reporter(seeded).failed_runs()}
    assert rows["R1"]["error"] == "second problem"
    assert rows["R2"]["error"] is None
    assert rows["R1"]["platform"] == "illumina"


def test_failed_runs_empty(seeded):
    assert Reporter(seeded).failed_runs() == []


def _gc_values(db, values, source="fastqc"):
    db.get_or_create_run("R1", "illumina", "/x")
    for i, v in enumerate(values):
        db.add_qc("R1", f"S{i}", "R1.percent_gc", v, source=source)


def test_qc_outliers_finds_the_odd_sample(db):
    _gc_values(db, [48, 49, 47, 48, 50, 48, 49, 30])
    out = Reporter(db).qc_outliers("R1.percent_gc")
    assert [o["sample"] for o in out] == ["S7"]
    assert out[0]["modified_z"] < -3.5 and out[0]["median"] == 48


def test_qc_outliers_guards(db):
    rep = Reporter(db)
    _gc_values(db, [48, 49, 47, 30])
    assert rep.qc_outliers("R1.percent_gc") == []  # fewer than min_samples
    assert len(rep.qc_outliers("R1.percent_gc", min_samples=4, threshold=1.0)) >= 1
    assert rep.qc_outliers("no.such.metric") == []


def test_qc_outliers_zero_mad_and_filters(db):
    _gc_values(db, [48, 48, 48, 48, 48, 30])
    assert Reporter(db).qc_outliers("R1.percent_gc") == []  # MAD == 0: no meaningful score
    assert Reporter(db).qc_outliers("R1.percent_gc", source="multiqc") == []
    assert Reporter(db).qc_outliers("R1.percent_gc", run_id="other") == []


def test_qc_table_pivots_metrics_per_sample(db):
    db.get_or_create_run("R1", "illumina", "/x")
    db.add_qc("R1", "S1", "R1.percent_gc", 48.0)
    db.add_qc("R1", "S1", "R1.total_sequences", 1000.0)
    db.add_qc("R1", "S2", "R1.percent_gc", 51.0)
    table = Reporter(db).qc_table("R1")
    assert table == [
        {"sample": "S1", "R1.percent_gc": 48.0, "R1.total_sequences": 1000.0},
        {"sample": "S2", "R1.percent_gc": 51.0},
    ]


def test_run_events_and_report(seeded):
    seeded.set_status("R1", "demultiplexing")
    seeded.log_event("R1", "warning", "slow disk")
    job = seeded.add_job("R1", "downstream", "nextflow run x")
    assert job > 0
    rep = Reporter(seeded)
    assert [e["level"] for e in rep.run_events("R1")] == ["info", "warning"]
    assert [e["message"] for e in rep.run_events("R1", level="warning")] == ["slow disk"]
    report = rep.run_report("R1")
    assert report["samples"] == 3 and report["fastq_files"] == 3
    assert report["jobs"] == [{"name": "downstream", "status": "pending", "exit_code": None}]
    assert report["events_by_level"] == {"info": 1, "warning": 1}
    assert report["qc_metrics"] == 0


def test_run_report_unknown_run(db):
    with pytest.raises(KeyError):
        Reporter(db).run_report("nope")


def test_format_table():
    assert format_table([]) == "(no rows)"
    text = format_table([{"a": 1, "b": 0.123456}, {"a": 22, "c": None}])
    lines = text.splitlines()
    assert lines[0].split() == ["a", "b", "c"]
    assert "0.1235" in lines[2]
    assert len(lines) == 4


def test_format_table_prints_whole_numbers_in_full():
    from seqflow.reports import format_table

    out = format_table([{"s": "A", "reads": 21155707.0, "gc": 48.123456}])
    assert "21155707" in out and "e+" not in out and "48.12" in out
