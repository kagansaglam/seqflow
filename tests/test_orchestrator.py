import subprocess

from seqflow.demux import IlluminaDemultiplexer
from seqflow.launcher import PipelineLauncher
from seqflow.notify import Notifier
from seqflow.orchestrator import Orchestrator
from tests.conftest import write_fastq


class Recorder:
    def __init__(self):
        self.msgs = []

    def send(self, level, subject, body):
        self.msgs.append((level, subject))


def fake_demux_ok(self, cmd):
    for s in ("S1", "S2", "S3"):
        write_fastq(self.out_dir / f"{s}_R1.fastq.gz")
    return subprocess.CompletedProcess(cmd, 0, "", "")


def fake_demux_fail(self, cmd):
    return subprocess.CompletedProcess(cmd, 3, "", "boom: bad BCL")


def make(db, tmp_path, monkeypatch, demux_fn, launch_code=0):
    monkeypatch.setattr(IlluminaDemultiplexer, "_run", demux_fn)
    rec = Recorder()
    launcher = PipelineLauncher(work_root=tmp_path / "pl")
    monkeypatch.setattr(launcher, "_spawn",
                        lambda cmd: subprocess.CompletedProcess(cmd, launch_code, "", ""))
    return Orchestrator(db, Notifier([rec], [rec]), launcher, output_root=tmp_path / "fastq"), rec


def test_happy_path(db, tmp_path, monkeypatch, illumina_run):
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok)
    assert orch.process_run(illumina_run, "illumina") == "done"
    s = db.run_summary(illumina_run.name)
    assert s["status"] == "done" and s["samples"] == 3
    assert (tmp_path / "fastq" / illumina_run.name / "md5sum.txt").exists()
    assert any(level == "info" for level, _ in rec.msgs)


def test_demux_failure_marks_failed_and_alerts(db, tmp_path, monkeypatch, illumina_run):
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_fail)
    assert orch.process_run(illumina_run, "illumina") == "failed"
    assert db.run_summary(illumina_run.name)["status"] == "failed"
    assert any(level == "error" for level, _ in rec.msgs)


def test_incomplete_run_is_warning_not_failure(db, tmp_path, monkeypatch, illumina_run):
    (illumina_run / "RTAComplete.txt").unlink()
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok)
    assert orch.process_run(illumina_run, "illumina") == "detected"
    assert [lvl for lvl, _ in rec.msgs] == ["warning"]


def test_bad_sample_sheet_fails_before_demux(db, tmp_path, monkeypatch, illumina_run):
    (illumina_run / "SampleSheet.csv").write_text("[Data]\nSample_ID,index\nA,ACGX\n")
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok)
    assert orch.process_run(illumina_run, "illumina") == "failed"
    assert any(level == "error" for level, _ in rec.msgs)


def test_downstream_failure_is_reported(db, tmp_path, monkeypatch, illumina_run):
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok, launch_code=1)
    assert orch.process_run(illumina_run, "illumina") == "failed"
    assert any(level == "error" for level, _ in rec.msgs)


def _events(db, run_id):
    from seqflow.reports import Reporter
    return Reporter(db).run_events(run_id)


def test_fastq_files_are_recorded_with_size_and_verification(db, tmp_path, monkeypatch, illumina_run):
    orch, _ = make(db, tmp_path, monkeypatch, fake_demux_ok)
    orch.process_run(illumina_run, "illumina")
    s = db.run_summary(illumina_run.name)
    assert s["fastq_files"] == 3 and s["samples"] == 3 and s["total_bytes"] > 0
    with db.session() as session:
        from sqlalchemy import select

        from seqflow.models import FastqFile
        files = list(session.scalars(select(FastqFile)))
    assert all(f.md5 and len(f.md5) == 32 and f.verified_at for f in files)
    assert {f.read for f in files} == {"R1"}


def test_processing_a_run_twice_is_safe(db, tmp_path, monkeypatch, illumina_run):
    orch, _ = make(db, tmp_path, monkeypatch, fake_demux_ok)
    assert orch.process_run(illumina_run, "illumina") == "done"
    db.set_status(illumina_run.name, "detected")
    assert orch.process_run(illumina_run, "illumina") == "done"
    s = db.run_summary(illumina_run.name)
    assert s["samples"] == 3 and s["fastq_files"] == 3


def test_polling_an_unfinished_run_does_not_flood_the_log(db, tmp_path, monkeypatch, illumina_run):
    (illumina_run / "RTAComplete.txt").unlink()
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok)
    for _ in range(5):
        assert orch.process_run(illumina_run, "illumina") == "detected"
    assert [e["level"] for e in _events(db, illumina_run.name)] == ["warning"]
    assert [lvl for lvl, _ in rec.msgs] == ["warning"]  # one notification, not five
    (illumina_run / "RTAComplete.txt").write_text("done")
    assert orch.process_run(illumina_run, "illumina") == "done"


def test_missing_demux_tool_is_reported_as_demux_error(db, tmp_path, monkeypatch, illumina_run):
    def not_installed(self, cmd):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    orch, _ = make(db, tmp_path, monkeypatch, not_installed)
    assert orch.process_run(illumina_run, "illumina") == "failed"
    err = [e for e in _events(db, illumina_run.name) if e["level"] == "error"]
    assert "cannot run bcl-convert" in err[0]["message"]


def test_unexpected_exception_marks_run_failed_instead_of_crashing(db, tmp_path, monkeypatch, illumina_run):
    def boom(self, cmd):
        raise RuntimeError("disk exploded")

    orch, rec = make(db, tmp_path, monkeypatch, boom)
    assert orch.process_run(illumina_run, "illumina") == "failed"
    err = [e for e in _events(db, illumina_run.name) if e["level"] == "error"]
    assert "unexpected RuntimeError: disk exploded" in err[0]["message"]
    assert any(level == "error" for level, _ in rec.msgs)


def test_recover_interrupted_resets_only_in_flight_runs(db, tmp_path, monkeypatch, illumina_run):
    orch, rec = make(db, tmp_path, monkeypatch, fake_demux_ok)
    for run_id, status in [("A", "demultiplexing"), ("B", "pipeline_running"),
                           ("C", "done"), ("D", "failed"), ("E", "detected")]:
        db.get_or_create_run(run_id, "illumina", f"/x/{run_id}")
        db.set_status(run_id, status)
    assert orch.recover_interrupted() == ["A", "B"]
    assert db.run_ids_with_status(["detected"]) == {"A", "B", "E"}
    assert db.run_ids_with_status(["done", "failed"]) == {"C", "D"}
    assert orch.recover_interrupted() == []
    assert any(level == "warning" for level, _ in rec.msgs)
