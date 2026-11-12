import zipfile

import pytest

from seqflow.exceptions import QCParseError
from seqflow.qc import (
    QCIngestor,
    find_fastqc_outputs,
    parse_fastqc_file,
    parse_fastqc_text,
    parse_multiqc_general_stats,
    split_sample_and_read,
)


def fastqc_text(filename="S1_S1_L001_R1_001.fastq.gz", total=1000, gc=48, length="150",
                quality=(30.0, 34.0, 36.0), status="pass"):
    rows = "\n".join(f"{i + 1}\t{q}\t{q}\t30\t36\t25\t38" for i, q in enumerate(quality))
    return (
        "##FastQC\t0.12.1\n"
        f">>Basic Statistics\t{status}\n#Measure\tValue\n"
        f"Filename\t{filename}\nFile type\tConventional base calls\n"
        f"Total Sequences\t{total}\nSequences flagged as poor quality\t2\n"
        f"Sequence length\t{length}\n%GC\t{gc}\n>>END_MODULE\n"
        ">>Per base sequence quality\twarn\n"
        "#Base\tMean\tMedian\tLower Quartile\tUpper Quartile\t10th Percentile\t90th Percentile\n"
        f"{rows}\n>>END_MODULE\n"
        ">>Adapter Content\tfail\n#Position\tIllumina Universal Adapter\n1\t0.0\n>>END_MODULE\n"
    )


@pytest.mark.parametrize("name,expected", [
    ("S1_S1_L001_R1_001.fastq.gz", ("S1", "R1")),
    ("S1_S1_L001_R2_001.fastq.gz", ("S1", "R2")),
    ("sampleA_R1.fastq.gz", ("sampleA", "R1")),
    ("sampleA_R2.fq.gz", ("sampleA", "R2")),
    ("sampleA_R1_fastqc.zip", ("sampleA", "R1")),
    ("single.fastq", ("single", "SE")),
])
def test_split_sample_and_read(name, expected):
    assert split_sample_and_read(name) == expected


def test_parse_fastqc_text_metrics():
    res = parse_fastqc_text(fastqc_text())
    assert (res.sample, res.read) == ("S1", "R1")
    m = res.metrics
    assert m["total_sequences"] == 1000 and m["percent_gc"] == 48
    assert m["poor_quality_sequences"] == 2
    assert m["min_seq_length"] == m["max_seq_length"] == 150
    assert m["mean_base_quality"] == pytest.approx(100 / 3)
    assert m["modules_failed"] == 1 and m["modules_warned"] == 1
    assert res.module_status["Adapter Content"] == "fail"


def test_sequence_length_range():
    res = parse_fastqc_text(fastqc_text(length="35-151"))
    assert res.metrics["min_seq_length"] == 35 and res.metrics["max_seq_length"] == 151


def test_rejects_non_fastqc_text():
    with pytest.raises(QCParseError, match="Basic Statistics"):
        parse_fastqc_text("hello\nworld\n")


def test_parse_dir_zip_and_plain_file(tmp_path):
    d = tmp_path / "S1_R1_fastqc"
    d.mkdir()
    (d / "fastqc_data.txt").write_text(fastqc_text("S1_R1.fastq.gz"))
    assert parse_fastqc_file(d).sample == "S1"
    assert parse_fastqc_file(d / "fastqc_data.txt").read == "R1"

    z = tmp_path / "S2_R2_fastqc.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("S2_R2_fastqc/fastqc_data.txt", fastqc_text("S2_R2.fastq.gz"))
    res = parse_fastqc_file(z)
    assert (res.sample, res.read) == ("S2", "R2")


def test_bad_inputs(tmp_path):
    (tmp_path / "bad.zip").write_bytes(b"not a zip")
    with pytest.raises(QCParseError, match="valid zip"):
        parse_fastqc_file(tmp_path / "bad.zip")
    empty = tmp_path / "empty_fastqc.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("readme.txt", "x")
    with pytest.raises(QCParseError, match="no fastqc_data.txt"):
        parse_fastqc_file(empty)
    with pytest.raises(QCParseError, match="not found"):
        parse_fastqc_file(tmp_path / "missing.txt")
    (tmp_path / "emptydir").mkdir()
    with pytest.raises(QCParseError):
        parse_fastqc_file(tmp_path / "emptydir")


def test_directory_wins_over_zip_twin(tmp_path):
    d = tmp_path / "S1_R1_fastqc"
    d.mkdir()
    (d / "fastqc_data.txt").write_text(fastqc_text("S1_R1.fastq.gz"))
    with zipfile.ZipFile(tmp_path / "S1_R1_fastqc.zip", "w") as zf:
        zf.writestr("S1_R1_fastqc/fastqc_data.txt", fastqc_text("S1_R1.fastq.gz"))
    with zipfile.ZipFile(tmp_path / "S2_R1_fastqc.zip", "w") as zf:
        zf.writestr("S2_R1_fastqc/fastqc_data.txt", fastqc_text("S2_R1.fastq.gz"))
    found = find_fastqc_outputs(tmp_path)
    assert [p.name for p in found] == ["S1_R1_fastqc", "S2_R1_fastqc.zip"]


MULTIQC = (
    "Sample\tFastQC_mqc-generalstats-fastqc-percent_duplicates\t"
    "FastQC_mqc-generalstats-fastqc-percent_gc\tFastQC_mqc-generalstats-fastqc-total_sequences\n"
    "S1_R1\t12.5\t48\t1000000\n"
    "S2_R1\t\t51\t900000\n"
)


def test_parse_multiqc_general_stats(tmp_path):
    f = tmp_path / "multiqc_general_stats.txt"
    f.write_text(MULTIQC)
    table = parse_multiqc_general_stats(f)
    assert table["S1_R1"] == {"percent_duplicates": 12.5, "percent_gc": 48, "total_sequences": 1e6}
    assert "percent_duplicates" not in table["S2_R1"]  # empty cell skipped


def test_multiqc_rejects_wrong_file(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("a\tb\n1\t2\n")
    with pytest.raises(QCParseError, match="Sample"):
        parse_multiqc_general_stats(f)


def test_ingest_fastqc_is_idempotent_and_per_read(db, tmp_path):
    db.get_or_create_run("R1", "illumina", "/x")
    for sample, read in [("S1", "R1"), ("S1", "R2"), ("S2", "R1")]:
        d = tmp_path / "qc" / f"{sample}_{read}_fastqc"
        d.mkdir(parents=True)
        (d / "fastqc_data.txt").write_text(fastqc_text(f"{sample}_{read}.fastq.gz"))
    ing = QCIngestor(db)
    first = ing.ingest_fastqc("R1", tmp_path / "qc")
    assert first == 3 * 8  # 8 metrics for each of 3 files
    assert ing.ingest_fastqc("R1", tmp_path / "qc") == first  # replaced, not duplicated
    assert db.run_summary("R1")["status"] == "detected"
    with db.session() as s:
        from sqlalchemy import func, select

        from seqflow.models import QCMetric
        assert s.scalar(select(func.count(QCMetric.id))) == first
        names = set(s.scalars(select(QCMetric.name)))
    assert "R1.total_sequences" in names and "R2.percent_gc" in names


def test_ingest_multiqc_and_sources_do_not_clash(db, tmp_path):
    db.get_or_create_run("R1", "illumina", "/x")
    f = tmp_path / "multiqc_general_stats.txt"
    f.write_text(MULTIQC)
    ing = QCIngestor(db)
    assert ing.ingest_multiqc("R1", f) == 5
    db.add_qc("R1", "S1", "R1.percent_gc", 1.0)  # a manual metric with a clashing name
    assert ing.ingest_multiqc("R1", f) == 5  # re-ingest leaves the manual row alone


def test_ingest_errors(db, tmp_path):
    db.get_or_create_run("R1", "illumina", "/x")
    with pytest.raises(QCParseError, match="no FastQC results"):
        QCIngestor(db).ingest_fastqc("R1", tmp_path)
    with pytest.raises(QCParseError, match="not a directory"):
        QCIngestor(db).ingest_fastqc("R1", tmp_path / "nope")
    with pytest.raises(KeyError):
        QCIngestor(db).ingest_multiqc("missing", _write(tmp_path / "m.txt", MULTIQC))


def _write(path, text):
    path.write_text(text)
    return path


# Header and values as written by MultiQC 1.35 for paired-end airway data (shortened).
REAL_MULTIQC = (
    "Sample\tfastqc-percent_duplicates\tfastqc-percent_gc\tfastqc-total_sequences\n"
    "SRR1039508_1\t50.2958558183271\t50.0\t22.935520999999998\n"
    "SRR1039508_2\t47.93919565810487\t50.0\t22.935520999999998\n"
    "SRR1039509_1\t49.59447294108932\t49.0\t21.155707\n"
)


def test_ingest_real_multiqc_names_mates_and_units(db, tmp_path):
    from sqlalchemy import select

    from seqflow.models import QCMetric, Sample
    db.get_or_create_run("R1", "illumina", "/x")
    f = _write(tmp_path / "multiqc_general_stats.txt", REAL_MULTIQC)
    assert QCIngestor(db).ingest_multiqc("R1", f) == 9
    with db.session() as s:
        rows = {(q.sample_id, q.name): q.value for q in s.scalars(select(QCMetric))}
        assert not list(s.scalars(select(Sample)))  # QC ingest creates no samples
    assert rows[("SRR1039508", "R2.percent_gc")] == 50.0
    assert rows[("SRR1039509", "R1.total_sequences_millions")] == 21.155707
    assert {sample for sample, _ in rows} == {"SRR1039508", "SRR1039509"}
