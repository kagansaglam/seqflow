"""Whole-system tests: real processes, real SQLite file, real files and checksums.

Only the vendor demultiplexer is replaced by a small stand-in script named
`bcl-convert` that writes FASTQ files for the samples in the sample sheet. Everything
else (watcher, lock, orchestrator, subprocess call, checksums, database, reports) is
the real code. This does NOT prove compatibility with the real bcl-convert.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seqflow.cli import main
from seqflow.db import Database
from seqflow.reports import Reporter

pytestmark = pytest.mark.skipif(os.name == "nt", reason="stand-in tool is a POSIX script")

STUB = """#!{python}
import gzip, random, sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("--output-directory") + 1])
sheet = Path(args[args.index("--sample-sheet") + 1]).read_text().splitlines()
start = next(i for i, line in enumerate(sheet) if line.startswith("[Data]"))
header = sheet[start + 1].split(",")
ids = [row.split(",")[header.index("Sample_ID")] for row in sheet[start + 2:] if row.strip()]
rng = random.Random(1)
out.mkdir(parents=True, exist_ok=True)
for n, sample in enumerate(ids + ["Undetermined"], start=1):
    for read in ("R1", "R2"):
        with gzip.open(out / f"{{sample}}_S{{n}}_L001_{{read}}_001.fastq.gz", "wb") as fh:
            fh.write(rng.randbytes(5000))
"""

SHEET = """[Header]
IEMFileVersion,5

[Data]
Sample_ID,Sample_Project,index,index2
S1,projA,ACGTACGT,TTGGCCAA
S2,projA,GGCCTTAA,AACCGGTT
S3,projB,CCAATTGG,GGTTAACC
"""


@pytest.fixture
def tools(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "bcl-convert"
    tool.write_text(STUB.format(python=sys.executable))
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    for key in ("SEQFLOW_SLACK_WEBHOOK", "SEQFLOW_SMTP_HOST", "SEQFLOW_MAIL_TO"):
        monkeypatch.delenv(key, raising=False)
    return bin_dir


def make_run(root: Path, name: str, complete: bool = True) -> Path:
    run = root / name
    run.mkdir(parents=True)
    (run / "SampleSheet.csv").write_text(SHEET)
    if complete:
        (run / "RTAComplete.txt").write_text("done")
    return run


def test_process_one_run_from_the_command_line(tools, tmp_path, capsys):
    run = make_run(tmp_path / "runs", "RUN1")
    url, out = f"sqlite:///{tmp_path / 'e2e.db'}", tmp_path / "fastq"
    code = main(["--db", url, "process", str(run), "--platform", "illumina", "--no-launch",
                 "--output-root", str(out)])
    assert code == 0
    capsys.readouterr()

    manifest = out / "RUN1" / "md5sum.txt"
    assert len(manifest.read_text().splitlines()) == 8  # 3 samples + Undetermined, R1 and R2

    db = Database(url)
    try:
        summary = db.run_summary("RUN1")
        assert summary["status"] == "done"
        assert summary["fastq_files"] == 8 and summary["total_bytes"] > 8 * 5000 - 800
        report = {r["project"]: r for r in Reporter(db).yield_by_project()}
        assert report["projA"]["samples"] == 2 and report["projB"]["fastq_files"] == 2
        from sqlalchemy import select

        from seqflow.models import FastqFile
        with db.session() as s:
            for f in s.scalars(select(FastqFile)):
                assert hashlib.md5(Path(f.path).read_bytes()).hexdigest() == f.md5
                assert f.verified_at is not None
    finally:
        db.engine.dispose()


def test_a_failing_tool_marks_the_run_failed_and_requeue_recovers_it(tools, tmp_path, capsys):
    run = make_run(tmp_path / "runs", "RUN2")
    url, out = f"sqlite:///{tmp_path / 'e2e.db'}", tmp_path / "fastq"
    broken = tools / "bcl-convert"
    good = broken.read_text()
    broken.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('Error: bad BCL')\nsys.exit(3)\n")
    args = ["--db", url, "process", str(run), "--platform", "illumina", "--no-launch",
            "--output-root", str(out)]
    assert main(args) == 1
    capsys.readouterr()
    assert main(["--db", url, "report", "failed"]) == 0
    assert "bcl-convert exited 3" in capsys.readouterr().out

    broken.write_text(good)  # the operator fixes the problem
    assert main(["--db", url, "requeue", "RUN2"]) == 0
    capsys.readouterr()
    assert main(args) == 0


def wait_for(predicate, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_unattended_watcher_handles_late_and_crashed_runs_then_stops_cleanly(tools, tmp_path):
    runs, out = tmp_path / "runs", tmp_path / "fastq"
    url = f"sqlite:///{tmp_path / 'daemon.db'}"
    make_run(runs, "RUN_CRASHED")
    late = make_run(runs, "RUN_LATE", complete=False)

    # A previous process died while RUN_CRASHED was being demultiplexed.
    seed = Database(url)
    seed.upgrade()
    seed.get_or_create_run("RUN_CRASHED", "illumina", str(runs / "RUN_CRASHED"))
    seed.set_status("RUN_CRASHED", "demultiplexing")
    seed.engine.dispose()

    proc = subprocess.Popen(
        [sys.executable, "-m", "seqflow.cli", "--db", url, "watch", str(runs), "--platform",
         "illumina", "--interval", "0.2", "--no-launch", "--output-root", str(out)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    db = Database(url)
    try:
        status = lambda rid: next(
            (r["status"] for r in _runs(db) if r["run_id"] == rid), None)
        assert wait_for(lambda: status("RUN_CRASHED") == "done"), "crashed run was not recovered"
        assert wait_for(lambda: bool(Reporter(db).run_events("RUN_LATE", "warning"))), \
            "unfinished run was not noticed"
        assert status("RUN_LATE") == "detected"

        # Let the watcher poll the unfinished run several more times.
        assert wait_for(lambda: beat_cycles(runs) >= 4), "watcher stopped scanning"

        (late / "RTAComplete.txt").write_text("done")  # the instrument finishes
        assert wait_for(lambda: status("RUN_LATE") == "done"), "late run was not picked up"

        # While running, a second instance on the same directory is refused.
        assert main(["--db", url, "watch", str(runs), "--platform", "illumina"]) == 1

        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        warnings = [e["message"] for e in Reporter(db).run_events("RUN_LATE", "warning")]
        crashed = [e["message"] for e in Reporter(db).run_events("RUN_CRASHED", "warning")]
        db.engine.dispose()

    assert proc.returncode == 0, err
    assert len(warnings) == 1  # polled at least 3 times while unfinished, logged once
    assert any("previous process stopped" in m for m in crashed)
    assert (out / "RUN_LATE" / "md5sum.txt").exists()
    assert not (runs / ".seqflow.lock").exists()
    beat = json.loads((runs / ".seqflow-heartbeat.json").read_text())
    assert beat["cycles"] >= 5


def beat_cycles(runs: Path) -> int:
    try:
        return json.loads((runs / ".seqflow-heartbeat.json").read_text())["cycles"]
    except (OSError, ValueError, KeyError):
        return 0


def _runs(db):
    from sqlalchemy import select

    from seqflow.models import Run

    with db.session() as s:
        return [{"run_id": r.run_id, "status": r.status} for r in s.scalars(select(Run))]
