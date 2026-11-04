import gzip
from pathlib import Path

import pytest

from seqflow.cli import main
from seqflow.db import Database
from seqflow.exceptions import FileOperationError
from seqflow.qc import split_sample_and_read
from seqflow.register import Registrar, count_fastq_reads
from seqflow.reports import Reporter


def write_reads(path: Path, n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"@r{i}\nACGTACGT\n+\nIIIIIIII\n" for i in range(n)).encode()
    with gzip.open(path, "wb") as fh:
        fh.write(body)


@pytest.mark.parametrize("name,expected", [
    ("SRR1234_1.fastq.gz", ("SRR1234", "R1")),
    ("SRR1234_2.fastq.gz", ("SRR1234", "R2")),
    ("SRR1234.fastq.gz", ("SRR1234", "SE")),
    ("S1_S1_L001_R2_001.fastq.gz", ("S1", "R2")),
    ("Undetermined_S0_L001_R1_001.fastq.gz", ("Undetermined", "R1")),
    ("S1_fastqc.zip", ("S1", "SE")),
])
def test_split_sample_and_read_handles_ena_and_illumina_names(name, expected):
    assert split_sample_and_read(name) == expected


def test_count_reads_plain_and_gzip(tmp_path):
    write_reads(tmp_path / "a.fastq.gz", 250)
    (tmp_path / "b.fastq").write_text("@r\nAC\n+\nII\n" * 3)
    assert count_fastq_reads(tmp_path / "a.fastq.gz", chunk_size=64) == 250
    assert count_fastq_reads(tmp_path / "b.fastq") == 3


def test_count_reads_accepts_concatenated_gzip_members(tmp_path):
    path = tmp_path / "c.fastq.gz"
    path.write_bytes(gzip.compress(b"@r\nAC\n+\nII\n") + gzip.compress(b"@s\nGT\n+\nII\n"))
    assert count_fastq_reads(path, chunk_size=16) == 2


def test_count_reads_rejects_truncated_corrupt_and_malformed(tmp_path):
    write_reads(tmp_path / "ok.fastq.gz", 100)
    raw = (tmp_path / "ok.fastq.gz").read_bytes()
    (tmp_path / "trunc.fastq.gz").write_bytes(raw[: len(raw) // 2])
    (tmp_path / "junk.fastq.gz").write_bytes(b"not gzip at all" * 10)
    (tmp_path / "odd.fastq.gz").write_bytes(gzip.compress(b"@r\nAC\n+\n"))
    for name, text in [("trunc", "truncated"), ("junk", "corrupt"), ("odd", "multiple of 4")]:
        with pytest.raises(FileOperationError, match=text):
            count_fastq_reads(tmp_path / f"{name}.fastq.gz")


def test_register_records_files_samples_and_read_counts(db, tmp_path):
    data = tmp_path / "data"
    write_reads(data / "SRR1_1.fastq.gz", 40)
    write_reads(data / "SRR1_2.fastq.gz", 40)
    write_reads(data / "SRR2_1.fastq.gz", 10)
    res = Registrar(db).register("PUB1", data, "illumina", project="public")
    assert (res.files, res.samples, res.total_reads, res.problems) == (3, 2, 90, [])
    assert db.run_summary("PUB1")["status"] == "done"
    assert db.run_summary("PUB1")["fastq_files"] == 3
    table = {r["sample"]: r for r in Reporter(db).qc_table("PUB1")}
    assert table["SRR1"]["R1.reads_counted"] == 40 and table["SRR2"]["R1.reads_counted"] == 10
    assert table["SRR1"]["R2.reads_counted"] == 40


def test_register_twice_does_not_duplicate(db, tmp_path):
    data = tmp_path / "data"
    write_reads(data / "A_1.fastq.gz", 5)
    reg = Registrar(db)
    reg.register("PUB2", data)
    reg.register("PUB2", data)
    assert db.run_summary("PUB2")["fastq_files"] == 1


def test_register_marks_run_failed_on_a_corrupt_file_but_keeps_good_ones(db, tmp_path):
    data = tmp_path / "data"
    write_reads(data / "good_1.fastq.gz", 5)
    (data / "bad_1.fastq.gz").write_bytes(b"garbage" * 20)
    res = Registrar(db).register("PUB3", data)
    assert res.files == 1 and len(res.problems) == 1
    assert db.run_summary("PUB3")["status"] == "failed"


def test_register_rejects_empty_or_missing_directory(db, tmp_path):
    with pytest.raises(FileOperationError):
        Registrar(db).register("X", tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileOperationError, match="no files"):
        Registrar(db).register("X", tmp_path / "empty")


def test_register_command_line(tmp_path, capsys):
    data = tmp_path / "data"
    write_reads(data / "SRR9_1.fastq.gz", 7)
    url = f"sqlite:///{tmp_path / 'r.db'}"
    assert main(["--db", url, "register", "PUB4", str(data), "--project", "public"]) == 0
    assert '"total_reads": 7' in capsys.readouterr().out
    assert main(["--db", url, "register", "PUB5", str(tmp_path / "missing")]) == 1
    db = Database(url)
    try:
        assert db.run_summary("PUB4")["status"] == "done"
    finally:
        db.engine.dispose()
