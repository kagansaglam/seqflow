"""Demultiplex FASTQ reads by barcode, and find the barcodes present in a read set.

Barcodes are taken either from a separate barcode/index read file (as in EMP 16S data,
or the I1 file bcl-convert writes with CreateFastqForIndexReads) or from the Illumina
read header (``@... 1:N:0:ACGTACGT``, for example in Undetermined files).

Mismatch tolerance is limited by the barcode set: with a minimum pairwise Hamming
distance d, up to (d - 1) // 2 errors can be corrected without ambiguity. Variants that
lie within the tolerance of two barcodes are never assigned.
"""
from __future__ import annotations

import csv
import gzip
import json
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from itertools import combinations
from pathlib import Path

from .exceptions import DemuxError, SampleSheetError

_BASES = "ACGTN"
_COMPLEMENT = str.maketrans("ACGTN", "TGCAN")
UNDETERMINED = "Undetermined"


def reverse_complement(seq: str) -> str:
    return seq.translate(_COMPLEMENT)[::-1]


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b, strict=True))


def _open_text(path: Path, mode: str = "rt"):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, mode, compresslevel=4) if "w" in mode else gzip.open(path, mode)
    return open(path, mode)


def read_fastq(path: str | Path) -> Iterator[tuple[str, str, str]]:
    """Yield (header, sequence, quality) records; header without the leading '@'."""
    path = Path(path)
    with _open_text(path) as fh:
        n = 0
        while True:
            head = fh.readline()
            if not head:
                return
            seq, plus, qual = fh.readline(), fh.readline(), fh.readline()
            n += 1
            if not head.startswith("@") or not plus.startswith("+") or not qual:
                raise DemuxError(f"{path.name}: malformed FASTQ record {n}")
            yield head[1:].rstrip("\n"), seq.rstrip("\n"), qual.rstrip("\n")


def read_id(header: str) -> str:
    """'M00176:65:...:1:1101:15589:1331 1:N:0:0' -> 'M00176:65:...:1:1101:15589:1331'."""
    rid = header.split(None, 1)[0]
    return rid[:-2] if rid.endswith(("/1", "/2", "/3")) else rid


def header_barcode(header: str) -> str:
    """Index sequence from an Illumina header; dual indexes are joined without '+'."""
    parts = header.split(None, 1)
    if len(parts) < 2:
        raise DemuxError(f"read header has no index field: {header[:60]}")
    return parts[1].rsplit(":", 1)[-1].replace("+", "").upper()


class BarcodeSet:
    """Sample -> barcode mapping with a mismatch-tolerant, collision-safe lookup."""

    def __init__(self, barcodes: dict[str, str], mismatches: int = 0) -> None:
        if not barcodes:
            raise SampleSheetError("no barcodes given")
        clean = {s: b.strip().upper() for s, b in barcodes.items()}
        for sample, bc in clean.items():
            if not bc or set(bc) - set("ACGT"):
                raise SampleSheetError(f"invalid barcode '{bc}' for {sample}")
        lengths = {len(b) for b in clean.values()}
        if len(lengths) > 1:
            raise SampleSheetError(f"barcodes have different lengths: {sorted(lengths)}")
        owners: dict[str, str] = {}
        for sample, bc in clean.items():
            if bc in owners:
                raise SampleSheetError(f"{owners[bc]} and {sample} share barcode {bc}")
            owners[bc] = sample
        self.barcodes = clean
        self.length = lengths.pop()
        self.min_distance = min(
            (hamming(a, b) for a, b in combinations(clean.values(), 2)), default=self.length)
        self.max_correctable = (self.min_distance - 1) // 2
        if mismatches < 0 or mismatches > self.max_correctable:
            raise SampleSheetError(
                f"{mismatches} mismatches requested, but the minimum distance between "
                f"barcodes is {self.min_distance}, so at most {self.max_correctable} "
                "can be corrected without ambiguity")
        self.mismatches = mismatches
        self._lookup = self._build_lookup()

    def _build_lookup(self) -> dict[str, str | None]:
        lookup: dict[str, str | None] = {bc: s for s, bc in self.barcodes.items()}
        exact = set(lookup)
        frontier = {bc: s for s, bc in self.barcodes.items()}
        for _ in range(self.mismatches):
            nxt: dict[str, str | None] = {}
            for seq, sample in frontier.items():
                for i, base in enumerate(seq):
                    for alt in _BASES:
                        if alt == base:
                            continue
                        var = seq[:i] + alt + seq[i + 1:]
                        if var in exact:
                            continue
                        if var in lookup and lookup[var] != sample:
                            lookup[var] = None  # within reach of two barcodes
                        elif var not in lookup:
                            lookup[var] = sample
                        nxt[var] = sample
            frontier = nxt
        return lookup

    def assign(self, observed: str) -> tuple[str | None, bool]:
        """(sample or None, exact match?)"""
        observed = observed[: self.length].upper()
        sample = self._lookup.get(observed)
        return sample, sample is not None and self.barcodes[sample] == observed

    def reverse_complemented(self) -> BarcodeSet:
        return BarcodeSet({s: reverse_complement(b) for s, b in self.barcodes.items()},
                          self.mismatches)


def read_sample_map(path: str | Path, barcode_column: str,
                    sample_column: str | None = None) -> dict[str, str]:
    """Read a tab- or comma-separated table (QIIME 2 sample metadata works as is: the
    first column is the sample ID and '#q2:types' rows are skipped)."""
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig").splitlines()
    delimiter = "\t" if text and "\t" in text[0] else ","
    rows = [ln for ln in text if ln.strip() and not ln.startswith("#q2:")]
    reader = csv.DictReader(rows, delimiter=delimiter)
    header = reader.fieldnames or []
    if barcode_column not in header:
        raise SampleSheetError(f"column '{barcode_column}' not in {path.name}: {header}")
    sample_column = sample_column or header[0]
    out: dict[str, str] = {}
    for row in reader:
        sample = (row.get(sample_column) or "").strip()
        if sample and not sample.startswith("#"):
            if sample in out:
                raise SampleSheetError(f"sample {sample} listed twice in {path.name}")
            out[sample] = (row.get(barcode_column) or "").strip()
    return out


def scan_barcodes(path: str | Path, from_header: bool = False, length: int | None = None,
                  limit: int | None = None) -> tuple[int, Counter]:
    """Count barcode sequences in a barcode/index FASTQ (or in read headers)."""
    counts: Counter = Counter()
    total = 0
    for header, seq, _ in read_fastq(path):
        bc = header_barcode(header) if from_header else seq.upper()
        counts[bc[:length] if length else bc] += 1
        total += 1
        if limit and total >= limit:
            break
    return total, counts


@dataclass
class DemuxStats:
    reads: int = 0
    assigned: int = 0
    exact: int = 0
    corrected: int = 0
    undetermined: int = 0
    orientation: str = "forward"
    mismatches: int = 0
    min_barcode_distance: int = 0
    seconds: float = 0.0
    per_sample: dict[str, int] = field(default_factory=dict)
    top_unknown: list[tuple[str, int]] = field(default_factory=list)

    @property
    def assigned_fraction(self) -> float:
        return self.assigned / self.reads if self.reads else 0.0


class FastqDemultiplexer:
    """Split one read file into one FASTQ per sample plus Undetermined."""

    def __init__(self, barcodes: BarcodeSet, out_dir: str | Path,
                 orientation: str = "auto", sniff_reads: int = 10_000) -> None:
        if orientation not in ("auto", "forward", "reverse-complement"):
            raise ValueError(f"unknown orientation: {orientation}")
        self.barcodes = barcodes
        self.out_dir = Path(out_dir)
        self.orientation = orientation
        self.sniff_reads = sniff_reads

    def _observed(self, reads: Path, barcode_reads: Path | None) -> Iterator[tuple]:
        """Yield ((header, seq, qual), observed barcode)."""
        if barcode_reads is None:
            for rec in read_fastq(reads):
                yield rec, header_barcode(rec[0])
            return
        bc_iter = read_fastq(barcode_reads)
        for n, rec in enumerate(read_fastq(reads), start=1):
            bc = next(bc_iter, None)
            if bc is None:
                raise DemuxError(f"{barcode_reads.name} has fewer records than {reads.name}")
            if read_id(bc[0]) != read_id(rec[0]):
                raise DemuxError(f"record {n}: read '{read_id(rec[0])}' and barcode "
                                 f"'{read_id(bc[0])}' are out of step")
            yield rec, bc[1]
        if next(bc_iter, None) is not None:
            raise DemuxError(f"{barcode_reads.name} has more records than {reads.name}")

    def detect_orientation(self, reads: Path, barcode_reads: Path | None) -> str:
        rc = self.barcodes.reverse_complemented()
        fwd = rev = 0
        for n, (_, bc) in enumerate(self._observed(reads, barcode_reads)):
            if n >= self.sniff_reads:
                break
            fwd += self.barcodes.assign(bc)[1]
            rev += rc.assign(bc)[1]
        if fwd == rev == 0:
            raise DemuxError(f"none of the first {self.sniff_reads} barcodes matches a sample "
                             "barcode in either orientation; check the barcode column")
        return "forward" if fwd >= rev else "reverse-complement"

    def run(self, reads: str | Path, barcode_reads: str | Path | None = None) -> DemuxStats:
        reads = Path(reads)
        barcode_reads = Path(barcode_reads) if barcode_reads else None
        start = time.perf_counter()
        orientation = self.orientation
        if orientation == "auto":
            orientation = self.detect_orientation(reads, barcode_reads)
        bset = self.barcodes if orientation == "forward" else self.barcodes.reverse_complemented()

        self.out_dir.mkdir(parents=True, exist_ok=True)
        stats = DemuxStats(orientation=orientation, mismatches=bset.mismatches,
                           min_barcode_distance=bset.min_distance,
                           per_sample={s: 0 for s in bset.barcodes})
        unknown: Counter = Counter()
        handles = {}
        try:
            for name in [*bset.barcodes, UNDETERMINED]:
                handles[name] = _open_text(self.out_dir / f"{name}.fastq.gz", "wt")
            for (header, seq, qual), bc in self._observed(reads, barcode_reads):
                sample, exact = bset.assign(bc)
                stats.reads += 1
                if sample is None:
                    stats.undetermined += 1
                    unknown[bc] += 1
                    target = UNDETERMINED
                else:
                    stats.assigned += 1
                    stats.per_sample[sample] += 1
                    stats.exact += exact
                    stats.corrected += not exact
                    target = sample
                handles[target].write(f"@{header}\n{seq}\n+\n{qual}\n")
        finally:
            for fh in handles.values():
                fh.close()
        stats.top_unknown = unknown.most_common(10)
        stats.seconds = round(time.perf_counter() - start, 2)
        report = {**asdict(stats), "assigned_fraction": round(stats.assigned_fraction, 4),
                  "reads_file": reads.name,
                  "barcodes_file": barcode_reads.name if barcode_reads else "read headers"}
        (self.out_dir / "demux_stats.json").write_text(json.dumps(report, indent=2))
        return stats
