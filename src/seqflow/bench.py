"""Measure md5 throughput of a directory of files.

Read-only by default: it never writes into the benchmarked directory. Archiving is
benchmarked only when an explicit destination is given.

Method: one warm-up read of all files (reported separately as the first read), then
`repeats` timed passes per worker count; the median is reported. The tool also flags
speedups that exceed what the worker count could explain, which usually means the
baseline row was not measured in steady state.
"""
from __future__ import annotations

import os
import platform
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .checksum import ChecksumManager
from .filemgr import FileManager

CACHE_NOTE = (
    "Timed passes read files already pulled into the OS page cache by the warm-up, so "
    "they measure CPU/hash throughput, not disk speed. first_read_seconds is the "
    "warm-up read: cold-ish if the files were not read recently."
)
SUPERLINEAR_SLACK = 1.25


def _mb_per_s(n_bytes: int, seconds: float) -> float:
    return round(n_bytes / 1e6 / seconds, 1) if seconds > 0 else 0.0


def speedup_warnings(rows: list[dict]) -> list[str]:
    """Flag rows whose speedup over the first row exceeds the ideal by a wide margin."""
    if not rows:
        return []
    base_workers = rows[0]["workers"]
    out = []
    for r in rows[1:]:
        ideal = r["workers"] / base_workers
        if r["speedup"] > ideal * SUPERLINEAR_SLACK:
            out.append(
                f"{r['workers']} workers show {r['speedup']}x speedup but at most ~{ideal:g}x "
                "is expected: the first row was probably not in steady state. Re-run before "
                "quoting a speedup."
            )
    return out


def benchmark(root: str | Path, *, workers: Sequence[int] = (1, 2, 4, 8),
              pattern: str = "*.fastq.gz", repeats: int = 3, warmup: bool = True,
              archive_to: str | Path | None = None,
              progress: Callable[[str], None] | None = None) -> dict:
    if repeats < 1:
        raise ValueError("repeats must be >= 1")
    say = progress or (lambda _msg: None)
    fm = FileManager()

    t0 = time.perf_counter()
    inv = fm.inventory(root, pattern)
    inventory_seconds = time.perf_counter() - t0
    files = list(fm.iter_files(root, pattern))
    say(f"{inv.n_files} files, {inv.total_bytes / 1e9:.2f} GB")

    first_read = None
    if warmup:
        say("warm-up read (all files, once) ...")
        t = time.perf_counter()
        ChecksumManager(workers=max(workers)).compute_many(files)
        first_read = round(time.perf_counter() - t, 3)

    rows = []
    for w in workers:
        say(f"md5 with {w} worker(s), {repeats} timed pass(es) ...")
        cm = ChecksumManager(workers=w)
        timings = []
        for _ in range(repeats):
            t = time.perf_counter()
            cm.compute_many(files)
            timings.append(time.perf_counter() - t)
        median = statistics.median(timings)
        rows.append({
            "workers": w,
            "seconds_first": round(timings[0], 3),
            "seconds_median": round(median, 3),
            "mb_per_s_median": _mb_per_s(inv.total_bytes, median),
        })
    for r in rows:
        r["speedup"] = round(rows[0]["seconds_median"] / r["seconds_median"], 2) \
            if r["seconds_median"] > 0 else 0.0

    warnings = speedup_warnings(rows)
    if not warmup:
        warnings.append("no warm-up: the first row includes the cold read and is not comparable.")

    archive = None
    if archive_to is not None:
        best = max(workers)
        say("verified archive copy ...")
        arch_fm = FileManager(checksums=ChecksumManager(workers=best))
        t = time.perf_counter()
        res = arch_fm.archive(root, archive_to, pattern=pattern)
        secs = time.perf_counter() - t
        archive = {"seconds": round(secs, 3), "verified": res.verified, "workers": best,
                   "mb_per_s": _mb_per_s(res.total_bytes, secs)}

    return {
        "root": str(root),
        "pattern": pattern,
        "n_files": inv.n_files,
        "total_bytes": inv.total_bytes,
        "inventory_seconds": round(inventory_seconds, 3),
        "first_read_seconds": first_read,
        "repeats": repeats,
        "md5": rows,
        "archive": archive,
        "warnings": warnings,
        "host": {"cpu_count": os.cpu_count(), "platform": platform.platform(),
                 "python": sys.version.split()[0]},
        "note": CACHE_NOTE,
    }


def format_markdown(result: dict) -> str:
    gb = result["total_bytes"] / 1e9
    lines = [
        (f"**{result['n_files']} files, {gb:.2f} GB** in `{result['root']}` "
         f"(pattern `{result['pattern']}`); inventory {result['inventory_seconds']} s"),
    ]
    if result["first_read_seconds"] is not None:
        fr = result["first_read_seconds"]
        lines.append(f"First read (warm-up): {fr} s ({_mb_per_s(result['total_bytes'], fr)} MB/s)")
    lines += [
        "",
        "| workers | first pass (s) | median (s) | MB/s (median) | speedup |",
        "|---:|---:|---:|---:|---:|",
    ]
    for r in result["md5"]:
        lines.append(f"| {r['workers']} | {r['seconds_first']} | {r['seconds_median']} "
                     f"| {r['mb_per_s_median']} | {r['speedup']}x |")
    if result["archive"]:
        a = result["archive"]
        lines += ["", (f"Verified archive copy: {a['seconds']} s ({a['mb_per_s']} MB/s, "
                       f"{a['workers']} workers, verified={a['verified']})")]
    for w in result["warnings"]:
        lines += ["", f"WARNING: {w}"]
    h = result["host"]
    lines += ["", f"Host: {h['cpu_count']} CPUs, {h['platform']}, Python {h['python']}", "",
              f"_{result['note']}_"]
    return "\n".join(lines)
