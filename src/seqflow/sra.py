"""Fetch many runs from the NCBI SRA in parallel and verify them.

For each run: `prefetch` downloads the .sra file, `fasterq-dump` writes FASTQ, the FASTQ
is gzipped while its records are counted, and the count is compared with the read count
the archive publishes (ENA's ``read_count``; for paired runs each mate must have it).
Every attempt is appended to a JSON-lines log. A run already logged as ``ok`` whose files
still exist is skipped, so an interrupted batch continues where it stopped.
"""
from __future__ import annotations

import csv
import gzip
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .exceptions import FetchError, ReadCountMismatchError


@dataclass(frozen=True)
class RunSpec:
    accession: str
    paired: bool
    expected_reads: int | None = None

    def outputs(self, out: Path) -> list[Path]:
        if self.paired:
            return [out / f"{self.accession}_1.fastq.gz", out / f"{self.accession}_2.fastq.gz"]
        return [out / f"{self.accession}.fastq.gz"]


@dataclass
class FetchResult:
    accession: str
    status: str  # ok | failed | skipped
    reads: int = 0
    bytes: int = 0
    attempts: int = 0
    seconds: float = 0.0
    error: str = ""


@dataclass
class BatchSummary:
    runs: int = 0
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    reads: int = 0
    bytes: int = 0
    seconds: float = 0.0
    failed_runs: list[str] = field(default_factory=list)


def read_run_list(path: str | Path) -> list[RunSpec]:
    """TSV/CSV with run_accession and optional library_layout and read_count columns
    (the ENA portal 'read_run' report works as is)."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    delimiter = "\t" if lines and "\t" in lines[0] else ","
    specs, seen = [], set()
    for row in csv.DictReader(lines, delimiter=delimiter):
        acc = (row.get("run_accession") or "").strip()
        if not acc:
            continue
        if acc in seen:
            raise FetchError(f"{acc} listed twice in {path.name}")
        seen.add(acc)
        count = (row.get("read_count") or "").strip()
        specs.append(RunSpec(acc, (row.get("library_layout") or "").upper() == "PAIRED",
                             int(count) if count else None))
    if not specs:
        raise FetchError(f"no run_accession rows in {path.name}")
    return specs


def gzip_and_count(src: Path, dest: Path, level: int = 4) -> int:
    """Gzip src to dest (atomically) and return the number of FASTQ records."""
    part = dest.with_name(dest.name + ".part")
    lines = 0
    with open(src, "rb") as fin, gzip.open(part, "wb", compresslevel=level) as fout:
        while chunk := fin.read(1 << 20):
            lines += chunk.count(b"\n")
            fout.write(chunk)
    if lines % 4:
        part.unlink()
        raise FetchError(f"{src.name}: {lines} lines is not a multiple of 4")
    part.replace(dest)
    return lines // 4


class FetchLog:
    """Append-only JSON-lines log, safe to write from several threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def done(self) -> set[str]:
        if not self.path.exists():
            return set()
        ok = set()
        for line in self.path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # a line cut off by a crash
            if rec.get("status") == "ok":
                ok.add(rec["accession"])
        return ok

    def write(self, result: FetchResult) -> None:
        with self._lock, open(self.path, "a") as fh:
            fh.write(json.dumps({**asdict(result), "time": time.strftime("%Y-%m-%dT%H:%M:%S")})
                     + "\n")


def find_tool(name: str, tools_dir: str | Path | None) -> str:
    if tools_dir:
        for candidate in (Path(tools_dir) / name, Path(tools_dir) / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    found = shutil.which(name)
    if not found:
        raise FetchError(f"{name} not found; install the SRA Toolkit or pass --tools-dir")
    return found


class SraFetcher:
    def __init__(self, out_dir: str | Path, work_dir: str | Path | None = None,
                 tools_dir: str | Path | None = None, workers: int = 4,
                 dump_threads: int = 2, retries: int = 3, backoff: float = 5.0,
                 log_path: str | Path | None = None,
                 on_result: Callable[[FetchResult, int, int], None] | None = None) -> None:
        self.out = Path(out_dir)
        self.work = Path(work_dir) if work_dir else self.out.parent / "sra_work"
        self.prefetch = find_tool("prefetch", tools_dir)
        self.dump = find_tool("fasterq-dump", tools_dir)
        self.workers, self.dump_threads = workers, dump_threads
        self.retries, self.backoff = retries, backoff
        self.log = FetchLog(Path(log_path) if log_path else self.out / "fetch_log.jsonl")
        self.on_result = on_result
        self.stop = threading.Event()

    def _run(self, cmd: list[str]) -> None:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode:
            tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise FetchError(f"{Path(cmd[0]).name} exited {proc.returncode}: {' | '.join(tail)}")

    def _downloaded(self, accession: str) -> Path | None:
        """The file prefetch wrote: <acc>.sra, or <acc>.sralite when NCBI only offers
        the SRA Lite format (simplified base qualities, same reads)."""
        for suffix in (".sra", ".sralite"):
            path = self.work / accession / f"{accession}{suffix}"
            if path.exists():
                return path
        return None

    def _fetch_once(self, spec: RunSpec) -> tuple[int, int]:
        self.out.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        sra = self._downloaded(spec.accession)
        if sra is None:
            self._run([self.prefetch, spec.accession, "-O", str(self.work), "--max-size", "100G"])
            sra = self._downloaded(spec.accession)
        if sra is None:
            found = sorted(p.name for p in (self.work / spec.accession).glob("*")) or ["nothing"]
            raise FetchError(f"prefetch left no .sra/.sralite file (found {', '.join(found)})")
        raw = self.work / f"{spec.accession}_fastq"
        shutil.rmtree(raw, ignore_errors=True)
        (raw / "tmp").mkdir(parents=True)  # own temp dir: parallel dumps must not share one
        self._run([self.dump, str(sra), "-O", str(raw), "-t", str(raw / "tmp"),
                   "--threads", str(self.dump_threads), "--force"])
        if spec.paired:
            sources = [raw / f"{spec.accession}_1.fastq", raw / f"{spec.accession}_2.fastq"]
        else:
            sources = [raw / f"{spec.accession}.fastq"]
        for src in sources:
            if not src.exists():
                produced = sorted(p.name for p in raw.glob("*")) or ["nothing"]
                raise FetchError(f"{src.name} not produced (got {', '.join(produced)}); "
                                 "check library_layout")
        counts, size = [], 0
        for src, dest in zip(sources, spec.outputs(self.out), strict=True):
            counts.append(gzip_and_count(src, dest))
            size += dest.stat().st_size
        shutil.rmtree(raw, ignore_errors=True)
        shutil.rmtree(self.work / spec.accession, ignore_errors=True)
        if spec.expected_reads is not None and any(c != spec.expected_reads for c in counts):
            for dest in spec.outputs(self.out):
                dest.unlink(missing_ok=True)
            raise ReadCountMismatchError(f"read count {counts} differs from the expected "
                                         f"{spec.expected_reads}")
        return counts[0], size

    def fetch(self, spec: RunSpec) -> FetchResult:
        start = time.perf_counter()
        result = FetchResult(spec.accession, "failed")
        for attempt in range(1, self.retries + 1):
            if self.stop.is_set():
                result.error = result.error or "stopped before completion"
                break
            result.attempts = attempt
            try:
                result.reads, result.bytes = self._fetch_once(spec)
                result.status, result.error = "ok", ""
                break
            except ReadCountMismatchError as exc:
                result.error = str(exc)
                break
            except (FetchError, OSError) as exc:
                result.error = str(exc)
                if attempt < self.retries and not self.stop.is_set():
                    self.stop.wait(self.backoff * 2 ** (attempt - 1))
        result.seconds = round(time.perf_counter() - start, 2)
        return result

    def run(self, specs: list[RunSpec]) -> BatchSummary:
        self.out.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        summary = BatchSummary(runs=len(specs))
        done = self.log.done()
        todo = []
        for spec in specs:
            if spec.accession in done and all(p.exists() for p in spec.outputs(self.out)):
                summary.skipped += 1
            else:
                todo.append(spec)
        finished = summary.skipped
        pool = ThreadPoolExecutor(max_workers=self.workers)
        try:
            futures = [pool.submit(self.fetch, spec) for spec in todo]
            for fut in as_completed(futures):
                res = fut.result()
                self.log.write(res)
                finished += 1
                if res.status == "ok":
                    summary.ok += 1
                    summary.reads += res.reads
                    summary.bytes += res.bytes
                else:
                    summary.failed += 1
                    summary.failed_runs.append(res.accession)
                if self.on_result:
                    self.on_result(res, finished, len(specs))
        except KeyboardInterrupt:
            self.stop.set()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        summary.failed_runs.sort()
        summary.seconds = round(time.perf_counter() - start, 1)
        return summary
