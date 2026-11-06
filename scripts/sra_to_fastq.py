"""Fetch runs from NCBI SRA and convert them to gzipped paired FASTQ files.

    python scripts/sra_to_fastq.py SRR1039508 SRR1039509 --out C:\\seqdata\\airway \\
        --tools-dir C:\\tools\\sratoolkit\\bin

Per run: `prefetch` downloads the .sra file, `fasterq-dump` writes <run>_1.fastq and
<run>_2.fastq, which are then gzipped (both at once) and the temporary files removed, so only one run
needs the large uncompressed space at a time. Runs whose two .fastq.gz files already
exist are skipped, so the command can be started again after an interruption.
Requires the NCBI SRA Toolkit. Standard library only.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def find_tool(name: str, tools_dir: str | None) -> str:
    if tools_dir:
        for candidate in (Path(tools_dir) / name, Path(tools_dir) / f"{name}.exe"):
            if candidate.exists():
                return str(candidate)
    found = shutil.which(name)
    if not found:
        raise SystemExit(f"{name} not found; install the SRA Toolkit or pass --tools-dir")
    return found


def gzip_file(src: Path, dest: Path, level: int = 4) -> None:
    part = dest.with_name(dest.name + ".part")
    with open(src, "rb") as fin, gzip.open(part, "wb", compresslevel=level) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
    part.replace(dest)


def convert_run(run: str, out: Path, work: Path, prefetch: str, dump: str, threads: int) -> str:
    finals = [out / f"{run}_1.fastq.gz", out / f"{run}_2.fastq.gz"]
    if all(p.exists() for p in finals):
        return "already complete"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    sra = work / run / f"{run}.sra"
    if not sra.exists():
        subprocess.run([prefetch, run, "-O", str(work), "--max-size", "100G"], check=True)
    raw = work / f"{run}_fastq"
    shutil.rmtree(raw, ignore_errors=True)
    subprocess.run([dump, str(sra), "-O", str(raw), "--threads", str(threads), "--force"],
                   check=True)
    sources = [raw / f"{run}_{n}.fastq" for n in (1, 2)]
    for fastq in sources:
        if not fastq.exists():
            raise SystemExit(f"{run}: {fastq.name} was not produced (single-end run?)")
    with ThreadPoolExecutor(max_workers=2) as pool:  # both mates at once (zlib frees the GIL)
        for future in [pool.submit(gzip_file, s, f) for s, f in zip(sources, finals)]:
            future.result()
    shutil.rmtree(raw, ignore_errors=True)
    shutil.rmtree(work / run, ignore_errors=True)
    return "converted"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("runs", nargs="+")
    p.add_argument("--out", required=True, help="directory for the .fastq.gz files")
    p.add_argument("--work", help="scratch directory (default: OUT/../sra_work)")
    p.add_argument("--tools-dir", help="folder with prefetch and fasterq-dump")
    p.add_argument("--threads", type=int, default=4)
    args = p.parse_args(argv)
    out = Path(args.out)
    work = Path(args.work) if args.work else out.parent / "sra_work"
    prefetch = find_tool("prefetch", args.tools_dir)
    dump = find_tool("fasterq-dump", args.tools_dir)
    for run in args.runs:
        print(f"{run}: ", end="", flush=True)
        print(convert_run(run, out, work, prefetch, dump, args.threads))
    return 0


if __name__ == "__main__":
    sys.exit(main())
