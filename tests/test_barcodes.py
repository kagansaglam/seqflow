import gzip
import json
import random
from pathlib import Path

import pytest

from seqflow.barcodes import (
    BarcodeSet,
    FastqDemultiplexer,
    hamming,
    header_barcode,
    read_fastq,
    read_sample_map,
    reverse_complement,
    scan_barcodes,
)
from seqflow.cli import main
from seqflow.db import Database
from seqflow.exceptions import DemuxError, SampleSheetError

# Three barcodes with pairwise distance >= 5, so up to 2 errors are correctable.
BC = {"s1": "AAAAAAAA", "s2": "CCCCCAAA", "s3": "GGGGGTTT"}


def mutate(seq: str, positions: list[int]) -> str:
    out = list(seq)
    for p in positions:
        out[p] = {"A": "T", "C": "G", "G": "A", "T": "C"}[out[p]]
    return "".join(out)


def write_pair(tmp: Path, barcodes: list[str], ids: list[str] | None = None) -> tuple[Path, Path]:
    ids = ids or [f"r{i}" for i in range(len(barcodes))]
    reads, bcs = tmp / "reads.fastq.gz", tmp / "barcodes.fastq.gz"
    with gzip.open(reads, "wt") as r, gzip.open(bcs, "wt") as b:
        for rid, bc in zip(ids, barcodes, strict=True):
            r.write(f"@{rid} 1:N:0:0\nACGTACGTA\n+\nIIIIIIIII\n")
            b.write(f"@{rid} 2:N:0:0\n{bc}\n+\n{'I' * len(bc)}\n")
    return reads, bcs


def test_lookup_corrects_up_to_the_allowed_mismatches():
    assert min(hamming(a, b) for a in BC.values() for b in BC.values() if a != b) == 5
    bset = BarcodeSet(BC, mismatches=2)
    assert bset.max_correctable == 2
    assert bset.assign("AAAAAAAA") == ("s1", True)
    assert bset.assign(mutate("CCCCCAAA", [0, 7])) == ("s2", False)
    assert bset.assign(mutate("GGGGGTTT", [0, 1, 2]))[0] is None  # 3 errors: too far
    assert bset.assign("AAAANAAA") == ("s1", False)  # N counts as a mismatch


def test_more_mismatches_than_the_set_allows_is_refused():
    with pytest.raises(SampleSheetError, match="at most 2"):
        BarcodeSet(BC, mismatches=3)
    close = {"a": "ACGTACGT", "b": "ACGTACGA"}  # distance 1
    with pytest.raises(SampleSheetError, match="at most 0"):
        BarcodeSet(close, mismatches=1)
    assert BarcodeSet(close).assign("ACGTACGA") == ("b", True)


@pytest.mark.parametrize("bad,msg", [
    ({"a": "ACGT", "b": "ACGTA"}, "different lengths"),
    ({"a": "ACGT", "b": "ACGT"}, "share barcode"),
    ({"a": "ACXT"}, "invalid barcode"),
    ({}, "no barcodes"),
])
def test_invalid_barcode_sets(bad, msg):
    with pytest.raises(SampleSheetError, match=msg):
        BarcodeSet(bad)


def test_demux_splits_reads_counts_and_reports_unknown(tmp_path):
    rng = random.Random(3)
    observed, expected = [], {"s1": 0, "s2": 0, "s3": 0}
    for _ in range(300):
        sample = rng.choice(list(BC))
        expected[sample] += 1
        observed.append(mutate(BC[sample], [rng.randrange(8)]) if rng.random() < 0.3
                        else BC[sample])
    observed += ["TTTTTTTT"] * 7 + ["ACACACAC"] * 2
    reads, bcs = write_pair(tmp_path, observed)
    stats = FastqDemultiplexer(BarcodeSet(BC, 1), tmp_path / "out").run(reads, bcs)
    assert stats.reads == 309 and stats.per_sample == expected
    assert stats.undetermined == 9 and stats.top_unknown[0] == ("TTTTTTTT", 7)
    assert stats.exact + stats.corrected == stats.assigned == 300 and stats.corrected > 0
    for sample, n in expected.items():
        assert sum(1 for _ in read_fastq(tmp_path / "out" / f"{sample}.fastq.gz")) == n
    assert sum(1 for _ in read_fastq(tmp_path / "out" / "Undetermined.fastq.gz")) == 9
    report = json.loads((tmp_path / "out" / "demux_stats.json").read_text())
    assert report["assigned_fraction"] == round(300 / 309, 4)


def test_reverse_complemented_barcodes_are_detected(tmp_path):
    observed = [reverse_complement(BC[s]) for s in ("s1", "s2", "s3") * 20]
    reads, bcs = write_pair(tmp_path, observed)
    stats = FastqDemultiplexer(BarcodeSet(BC), tmp_path / "out").run(reads, bcs)
    assert stats.orientation == "reverse-complement" and stats.assigned == 60
    forced = FastqDemultiplexer(BarcodeSet(BC), tmp_path / "o2", orientation="forward")
    assert forced.run(reads, bcs).assigned == 0


def test_no_match_in_either_orientation_is_an_error(tmp_path):
    reads, bcs = write_pair(tmp_path, ["ACACACAC"] * 5)  # not a barcode, nor its complement
    with pytest.raises(DemuxError, match="either orientation"):
        FastqDemultiplexer(BarcodeSet(BC), tmp_path / "out").run(reads, bcs)


def test_out_of_step_and_length_mismatch_are_detected(tmp_path):
    reads, _ = write_pair(tmp_path, [BC["s1"]] * 3)
    other = tmp_path / "other"
    other.mkdir()
    _, shifted = write_pair(other, [BC["s1"]] * 3, ids=["r0", "rX", "r2"])
    demux = FastqDemultiplexer(BarcodeSet(BC), tmp_path / "out", orientation="forward")
    with pytest.raises(DemuxError, match="out of step"):
        demux.run(reads, shifted)
    _, short = write_pair(other, [BC["s1"]] * 2)
    with pytest.raises(DemuxError, match="fewer records"):
        demux.run(reads, short)


def test_index_from_illumina_header(tmp_path):
    assert header_barcode("A00:1:FC:1:1101:1:1 1:N:0:ACGTACGT+GGTTAACC") == "ACGTACGTGGTTAACC"
    with pytest.raises(DemuxError):
        header_barcode("no_index_field")
    fq = tmp_path / "Undetermined_S0_L001_R1_001.fastq.gz"
    with gzip.open(fq, "wt") as fh:
        for i, bc in enumerate(["AAAAAAAA", "CCCCCAAA", "AAAAAAAT", "TTTTTTTT"]):
            fh.write(f"@r{i} 1:N:0:{bc}\nACGT\n+\nIIII\n")
    stats = FastqDemultiplexer(BarcodeSet(BC, 1), tmp_path / "out").run(fq)
    assert stats.per_sample == {"s1": 2, "s2": 1, "s3": 0} and stats.undetermined == 1
    total, counts = scan_barcodes(fq, from_header=True)
    assert total == 4 and counts["AAAAAAAA"] == 1


def test_qiime_style_sample_map(tmp_path):
    f = tmp_path / "meta.tsv"
    f.write_text("sample-id\tbarcode-sequence\tbody-site\n"
                 "#q2:types\tcategorical\tcategorical\n"
                 "L1S8\tAGCTGACTAGTC\tgut\nL1S57\tACACACTATGGC\tgut\n")
    assert read_sample_map(f, "barcode-sequence") == {"L1S8": "AGCTGACTAGTC",
                                                       "L1S57": "ACACACTATGGC"}
    with pytest.raises(SampleSheetError, match="not in"):
        read_sample_map(f, "BarcodeSequence")


def test_command_line_demux_register_and_scan(tmp_path, capsys):
    reads, bcs = write_pair(tmp_path, [BC["s1"]] * 4 + [BC["s3"]] * 2 + ["TTTTTTTT"])
    sheet = tmp_path / "map.tsv"
    sheet.write_text("sample\tbarcode\n" + "".join(f"{s}\t{b}\n" for s, b in BC.items()))
    url = f"sqlite:///{tmp_path / 'd.db'}"
    code = main(["--db", url, "demux-fastq", str(reads), "--barcode-reads", str(bcs),
                 "--sample-map", str(sheet), "--barcode-column", "barcode",
                 "--out", str(tmp_path / "out"), "--register-run", "MP1"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"undetermined": 1' in out
    db = Database(url)
    try:
        summary = db.run_summary("MP1")
        assert summary["status"] == "done" and summary["fastq_files"] == 4  # 3 + Undetermined
    finally:
        db.engine.dispose()
    assert main(["barcode-scan", str(bcs), "--top", "2"]) == 0
    scan = capsys.readouterr().out
    assert "7 reads, 3 distinct barcodes" in scan and "AAAAAAAA" in scan
