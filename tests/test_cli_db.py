import json

import pytest

from seqflow.cli import main
from seqflow.db import Database
from tests.test_qc import fastqc_text


@pytest.fixture
def url(tmp_path):
    return f"sqlite:///{tmp_path / 'cli.db'}"


def run_cli(url, *args):
    return main(["--db", url, *args])


def seed(url):
    db = Database(url)
    db.upgrade()
    db.get_or_create_run("R1", "illumina", "/x")
    db.add_sample("R1", "S1", "projA", "AAAA", "CCCC", 1)
    db.add_fastq("R1", "S1", "/o/S1.fastq.gz", "R1", 123, "a" * 32)
    db.log_event("R1", "error", "demux exploded")
    db.set_status("R1", "failed")
    db.engine.dispose()


def test_db_commands(url, capsys):
    assert run_cli(url, "db", "current") == 0
    assert "None" in capsys.readouterr().out
    assert run_cli(url, "db", "upgrade") == 0
    assert "0002" in capsys.readouterr().out
    assert run_cli(url, "db", "downgrade", "0001") == 0
    assert "0001" in capsys.readouterr().out
    assert run_cli(url, "db", "upgrade", "--revision", "head") == 0
    assert "0002" in capsys.readouterr().out


def test_report_commands(url, capsys):
    seed(url)
    assert run_cli(url, "report", "status") == 0
    assert "failed" in capsys.readouterr().out

    assert run_cli(url, "report", "--format", "json", "yield") == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["project"] == "projA" and rows[0]["total_bytes"] == 123

    assert run_cli(url, "report", "yield", "--since", "2020-01-01") == 0
    capsys.readouterr()

    assert run_cli(url, "report", "failed") == 0
    assert "demux exploded" in capsys.readouterr().out

    assert run_cli(url, "report", "events", "R1", "--level", "error") == 0
    assert "demux exploded" in capsys.readouterr().out

    assert run_cli(url, "report", "run", "R1") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    assert run_cli(url, "report", "qc", "R1") == 0
    assert "(no rows)" in capsys.readouterr().out
    assert run_cli(url, "report", "outliers", "R1.percent_gc") == 0
    assert "(no rows)" in capsys.readouterr().out


def test_qc_ingest_then_report(url, tmp_path, capsys):
    seed(url)
    qc = tmp_path / "qc" / "S1_R1_fastqc"
    qc.mkdir(parents=True)
    (qc / "fastqc_data.txt").write_text(fastqc_text("S1_R1.fastq.gz"))
    assert run_cli(url, "qc-ingest", "R1", "--fastqc-dir", str(tmp_path / "qc")) == 0
    assert json.loads(capsys.readouterr().out)["metrics_stored"] == {"fastqc": 8}
    assert run_cli(url, "report", "qc", "R1") == 0
    assert "R1.total_sequences" in capsys.readouterr().out


def test_errors_are_reported_not_raised(url, tmp_path, capsys):
    seed(url)
    assert run_cli(url, "qc-ingest", "R1") == 1  # nothing to ingest
    assert "--fastqc-dir" in capsys.readouterr().err
    assert run_cli(url, "qc-ingest", "R1", "--fastqc-dir", str(tmp_path / "nope")) == 1
    assert "not a directory" in capsys.readouterr().err
    assert run_cli(url, "report", "events", "NOPE") == 0  # no events is not an error
    capsys.readouterr()
    assert run_cli(url, "summary", "NOPE") == 1
    assert "unknown run" in capsys.readouterr().err
    assert run_cli(url, "inventory", str(tmp_path / "missing")) == 1
    assert "not a directory" in capsys.readouterr().err
