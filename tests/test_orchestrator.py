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
