"""Register FASTQ files that already exist (downloaded or demultiplexed elsewhere).

For every file: md5, size, gzip integrity check and read count are computed and stored
in the database, attached to a run and a sample derived from the file name. Nothing is
copied or modified. Running it again on the same directory updates rows, never
duplicates them.
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .checksum import ChecksumManager
from .db import Database
from .exceptions import FileOperationError
from .qc import split_sample_and_read

QC_SOURCE = "seqflow"


@dataclass
class RegisterResult:
    run_id: str
    files: int = 0
    samples: int = 0
    total_bytes: int = 0
    total_reads: int = 0
    problems: list[str] = field(default_factory=list)


def count_fastq_reads(path: str | Path, chunk_size: int = 1 << 20) -> int:
    """Stream-decompress a FASTQ(.gz) file and return its number of records.

    Raises FileOperationError for a corrupt/truncated gzip stream or a line count that
    is not a multiple of four. The file is read once, without parsing sequences."""
    path = Path(path)
    lines = 0
    last_byte = b"\n"
    gz = path.name.lower().endswith(".gz")
    try:
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if gz else None
        with open(path, "rb") as fh:
            while chunk := fh.read(chunk_size):
                data = decomp.decompress(chunk) if decomp else chunk
                while decomp and decomp.unused_data:  # concatenated gzip members
                    rest = decomp.unused_data
                    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    data += decomp.decompress(rest)
                if data:
                    lines += data.count(b"\n")
                    last_byte = data[-1:]
            if decomp:
                tail = decomp.flush()
                if tail:
                    lines += tail.count(b"\n")
                    last_byte = tail[-1:]
                if not decomp.eof:
                    raise FileOperationError(f"{path.name}: truncated gzip stream")
    except zlib.error as exc:
        raise FileOperationError(f"{path.name}: corrupt gzip data ({exc})") from exc
    if last_byte != b"\n":
        lines += 1
    if lines % 4:
        raise FileOperationError(f"{path.name}: {lines} lines is not a multiple of 4")
    return lines // 4


class Registrar:
    def __init__(self, db: Database, checksums: ChecksumManager | None = None) -> None:
        self.db = db
        self.checksums = checksums or ChecksumManager()

    def register(self, run_id: str, directory: str | Path, platform: str = "illumina",
                 project: str | None = None, pattern: str = "*.f*q.gz") -> RegisterResult:
        directory = Path(directory)
        if not directory.is_dir():
            raise FileOperationError(f"not a directory: {directory}")
        files = sorted(p for p in directory.rglob(pattern) if p.is_file() and not p.is_symlink())
        if not files:
            raise FileOperationError(f"no files matching {pattern} under {directory}")

        self.db.get_or_create_run(run_id, platform, str(directory.resolve()))
        self.db.set_status(run_id, "verified")
        sums = self.checksums.compute_many(files)
        result = RegisterResult(run_id)
        samples: set[str] = set()
        reads_per: dict[tuple[str, str], int] = {}
        for f in files:
            sample, read = split_sample_and_read(f.name)
            try:
                reads_per[(sample, read)] = count_fastq_reads(f)
            except FileOperationError as exc:
                result.problems.append(str(exc))
                continue
            if sample not in samples:
                self.db.add_sample(run_id, sample, project, None, None, None)
                samples.add(sample)
            size = f.stat().st_size
            self.db.add_fastq(run_id, sample, str(f.resolve()), read, size, sums[str(f)],
                              verified=True)
            result.files += 1
            result.total_bytes += size

        self.db.replace_qc(run_id, QC_SOURCE, [
            (sample, f"{read}.reads_counted", float(n))
            for (sample, read), n in sorted(reads_per.items())])
        result.samples = len(samples)
        result.total_reads = sum(reads_per.values())

        if result.problems:
            self.db.log_event(run_id, "error", "; ".join(result.problems[:5]))
            self.db.set_status(run_id, "failed")
        else:
            self.db.log_event(run_id, "info",
                              f"registered {result.files} files, {result.total_bytes} bytes")
            self.db.set_status(run_id, "done")
        return result
