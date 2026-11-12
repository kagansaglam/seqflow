"""Parse FastQC and MultiQC output and load it into the qc_metrics table.

FastQC metrics are stored per read, e.g. ``R1.total_sequences``. MultiQC general
statistics are stored with their own column names (prefix stripped), per sample.
Ingestion is idempotent: re-ingesting a source replaces its previous rows for the run.
"""
from __future__ import annotations

import csv
import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .exceptions import QCParseError

_READ_NAME = re.compile(
    r"^(?P<sample>.+?)(?:_S\d+)?(?:_L\d{3})?_(?P<read>R?[12])(?:_001)?$"
)
_FASTQ_SUFFIX = re.compile(r"\.(fastq|fq)(\.gz)?$", re.IGNORECASE)


@dataclass
class FastQCResult:
    sample: str
    read: str
    filename: str
    metrics: dict[str, float] = field(default_factory=dict)
    module_status: dict[str, str] = field(default_factory=dict)


def split_sample_and_read(filename: str) -> tuple[str, str]:
    """'S1_S1_L001_R2_001.fastq.gz' -> ('S1', 'R2'), 'SRR1_2.fastq.gz' -> ('SRR1', 'R2');
    names without a read number give read 'SE'."""
    stem = re.sub(r"\.zip$", "", Path(filename).name)
    stem = re.sub(r"_fastqc$", "", stem)
    stem = _FASTQ_SUFFIX.sub("", stem)
    m = _READ_NAME.match(stem)
    if not m:
        return stem, "SE"
    read = m.group("read")  # ENA/SRA style 'SRR123_1.fastq.gz' means R1
    return m.group("sample"), read if read.startswith("R") else f"R{read}"


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_fastqc_text(text: str, name_hint: str = "") -> FastQCResult:
    """Parse the contents of a fastqc_data.txt file."""
    modules: dict[str, list[str]] = {}
    status: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(">>END_MODULE"):
            current = None
        elif line.startswith(">>"):
            name, _, st = line[2:].partition("\t")
            current = name
            status[name] = st.strip().lower()
            modules[name] = []
        elif current is not None and line and not line.startswith("#"):
            modules[current].append(line)
    if "Basic Statistics" not in modules:
        raise QCParseError("no 'Basic Statistics' module: not a fastqc_data.txt file")

    basic = {}
    for row in modules["Basic Statistics"]:
        key, _, value = row.partition("\t")
        basic[key.strip()] = value.strip()

    filename = basic.get("Filename") or name_hint
    if not filename:
        raise QCParseError("cannot determine the sample: no Filename in the report")
    sample, read = split_sample_and_read(filename)
    res = FastQCResult(sample=sample, read=read, filename=filename, module_status=status)

    def put(name: str, value: float | None) -> None:
        if value is not None:
            res.metrics[name] = value

    put("total_sequences", _number(basic.get("Total Sequences", "")))
    put("poor_quality_sequences", _number(basic.get("Sequences flagged as poor quality", "")))
    put("percent_gc", _number(basic.get("%GC", "")))
    lengths = [_number(x) for x in re.split(r"-", basic.get("Sequence length", ""))]
    if lengths and all(x is not None for x in lengths):
        put("min_seq_length", min(lengths))
        put("max_seq_length", max(lengths))

    means = []
    for row in modules.get("Per base sequence quality", []):
        cols = row.split("\t")
        if len(cols) > 1 and (v := _number(cols[1])) is not None:
            means.append(v)
    if means:
        put("mean_base_quality", sum(means) / len(means))  # unweighted over positions

    res.metrics["modules_failed"] = float(sum(1 for s in status.values() if s == "fail"))
    res.metrics["modules_warned"] = float(sum(1 for s in status.values() if s == "warn"))
    return res


def parse_fastqc_file(path: str | Path) -> FastQCResult:
    """Accepts fastqc_data.txt, an unpacked *_fastqc directory, or a *_fastqc.zip."""
    path = Path(path)
    if path.is_dir():
        data = path / "fastqc_data.txt"
        if not data.is_file():
            raise QCParseError(f"no fastqc_data.txt in {path}")
        return parse_fastqc_text(data.read_text(), name_hint=path.name)
    if path.suffix == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                members = [n for n in zf.namelist() if n.endswith("fastqc_data.txt")]
                if not members:
                    raise QCParseError(f"no fastqc_data.txt inside {path.name}")
                return parse_fastqc_text(
                    zf.read(members[0]).decode("utf-8", "replace"),
                    name_hint=path.name[:-len(".zip")],
                )
        except zipfile.BadZipFile as exc:
            raise QCParseError(f"not a valid zip file: {path.name}") from exc
    if not path.is_file():
        raise QCParseError(f"not found: {path}")
    return parse_fastqc_text(path.read_text(), name_hint=path.parent.name)


def find_fastqc_outputs(qc_dir: str | Path) -> list[Path]:
    """FastQC results below qc_dir; an unpacked directory wins over its zip twin."""
    qc_dir = Path(qc_dir)
    if not qc_dir.is_dir():
        raise QCParseError(f"not a directory: {qc_dir}")
    dirs = sorted({p.parent for p in qc_dir.rglob("fastqc_data.txt")})
    seen = {(d.parent, d.name) for d in dirs}
    zips = sorted(z for z in qc_dir.rglob("*_fastqc.zip")
                  if (z.parent, z.name[:-len(".zip")]) not in seen)
    return dirs + zips


def parse_multiqc_general_stats(path: str | Path) -> dict[str, dict[str, float]]:
    """Read multiqc_general_stats.txt: {sample: {metric: value}}."""
    path = Path(path)
    if not path.is_file():
        raise QCParseError(f"not found: {path}")
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = next(reader, None)
        if not header or header[0] != "Sample":
            raise QCParseError("first column must be 'Sample': not a MultiQC general stats table")
        names = [h.rsplit("-", 1)[-1] if "-" in h else h for h in header[1:]]
        out: dict[str, dict[str, float]] = {}
        for row in reader:
            if not row or not row[0]:
                continue
            metrics = {}
            for name, cell in zip(names, row[1:], strict=False):
                if (v := _number(cell)) is not None:
                    metrics[name] = v
            out[row[0]] = metrics
    return out


class QCIngestor:
    def __init__(self, db: Database) -> None:
        self.db = db

    def ingest_fastqc(self, run_id: str, qc_dir: str | Path) -> int:
        outputs = find_fastqc_outputs(qc_dir)
        if not outputs:
            raise QCParseError(f"no FastQC results found below {qc_dir}")
        rows = []
        for out in outputs:
            res = parse_fastqc_file(out)
            for name, value in res.metrics.items():
                rows.append((res.sample, f"{res.read}.{name}", value))
        return self.db.replace_qc(run_id, "fastqc", rows)

    def ingest_multiqc(self, run_id: str, general_stats: str | Path) -> int:
        """Store MultiQC general statistics with the same sample and read naming as
        ingest_fastqc: a MultiQC row 'SRR1_2' becomes sample 'SRR1', metric 'R2.<name>'.
        MultiQC reports total_sequences in millions; it is stored as
        'total_sequences_millions' so the unit is part of the name."""
        table = parse_multiqc_general_stats(general_stats)
        rows = []
        for label, metrics in table.items():
            sample, read = split_sample_and_read(label)
            for name, value in metrics.items():
                if name == "total_sequences":
                    name = "total_sequences_millions"
                rows.append((sample, name if read == "SE" else f"{read}.{name}", value))
        return self.db.replace_qc(run_id, "multiqc", rows)
