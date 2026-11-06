"""Download run FASTQ files from the European Nucleotide Archive and verify them.

    python scripts/fetch_ena.py SRR1039508 SRR1039509 --out data/airway

File names, URLs and md5 sums come from the ENA portal API. A partial download is
resumed, and every file is checked against the md5 that ENA publishes, so plain http
(the default; some networks block https to the ENA file server) is safe. Use
--scheme https where it works. Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://www.ebi.ac.uk/ena/portal/api/filereport"


def file_list(run: str, api: str = API) -> list[tuple[str, str]]:
    query = urllib.parse.urlencode(
        {"accession": run, "result": "read_run", "fields": "fastq_ftp,fastq_md5", "format": "tsv"})
    with urllib.request.urlopen(f"{api}?{query}", timeout=60) as resp:
        lines = resp.read().decode().strip().splitlines()
    if len(lines) < 2:
        raise SystemExit(f"{run}: not found at ENA")
    header = lines[0].split("\t")
    row = dict(zip(header, lines[1].split("\t")))
    urls, sums = row.get("fastq_ftp", ""), row.get("fastq_md5", "")
    if not urls:
        raise SystemExit(f"{run}: ENA lists no FASTQ files")
    return list(zip(urls.split(";"), sums.split(";")))


def md5_of(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def with_scheme(url: str, scheme: str) -> str:
    """ENA lists URLs without a scheme ('ftp.sra.ebi.ac.uk/vol1/...')."""
    return f"{scheme}://" + url.split("://", 1)[-1]


def download(url: str, dest: Path, expected_md5: str, retries: int = 5) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and md5_of(dest) == expected_md5:
        return "already complete"
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if have and resp.status != 206:  # server ignored Range: start over
                    have = 0
                with open(part, "ab" if have else "wb") as out:
                    while chunk := resp.read(1 << 20):
                        out.write(chunk)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 416:  # already fully downloaded
                break
            print(f"  attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
    else:
        raise SystemExit(f"giving up on {url}")
    if md5_of(part) != expected_md5:
        part.unlink()
        raise SystemExit(f"md5 mismatch for {dest.name}; partial file removed, run again")
    part.replace(dest)
    return "downloaded, md5 ok"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("runs", nargs="+", help="run accessions, e.g. SRR1039508")
    p.add_argument("--out", required=True, help="destination directory")
    p.add_argument("--scheme", choices=["http", "https"], default="http",
                   help="URL scheme for the file server (default: http)")
    args = p.parse_args(argv)
    out = Path(args.out)
    for run in args.runs:
        for url, md5 in file_list(run):
            name = url.rsplit("/", 1)[-1]
            print(f"{name}: ", end="", flush=True)
            print(download(with_scheme(url, args.scheme), out / name, md5))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
