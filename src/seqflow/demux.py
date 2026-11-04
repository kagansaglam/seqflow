"""Demultiplexers. Abstract base + platform-specific subclasses.

The actual tool invocation goes through `_run`, which tests replace with a fake,
so no sequencer data or vendor binaries are needed for unit tests.
"""
from __future__ import annotations

import abc
import subprocess
from pathlib import Path

from .exceptions import DemuxError, RunIncompleteError
from .samplesheet import SampleSheet


class Demultiplexer(abc.ABC):
    platform: str = ""

    def __init__(self, run_dir: str | Path, out_dir: str | Path, sample_sheet: SampleSheet | None = None,
                 threads: int = 8) -> None:
        self.run_dir = Path(run_dir)
        self.out_dir = Path(out_dir)
        self.sample_sheet = sample_sheet
        self.threads = threads

    @abc.abstractmethod
    def build_command(self) -> list[str]:
        """Return the argv list for the demultiplexing tool."""

    @abc.abstractmethod
    def is_run_complete(self) -> bool:
        """True when the instrument has finished writing the run."""

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def run(self) -> list[Path]:
        if not self.is_run_complete():
            raise RunIncompleteError(f"run not complete: {self.run_dir}")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_command()
        try:
            proc = self._run(cmd)
        except OSError as exc:  # tool not installed, not executable, ...
            raise DemuxError(f"cannot run {cmd[0]}: {exc}") from exc
        if proc.returncode != 0:
            raise DemuxError(f"{cmd[0]} exited {proc.returncode}: {proc.stderr[-500:]}")
        fastqs = sorted(self.out_dir.rglob("*.fastq.gz"))
        if not fastqs:
            raise DemuxError("demultiplexing finished but no FASTQ files were produced")
        return fastqs


class IlluminaDemultiplexer(Demultiplexer):
    platform = "illumina"

    def is_run_complete(self) -> bool:
        return (self.run_dir / "RTAComplete.txt").exists()

    def build_command(self) -> list[str]:
        sheet = self.run_dir / "SampleSheet.csv"
        return [
            "bcl-convert",
            "--bcl-input-directory", str(self.run_dir),
            "--output-directory", str(self.out_dir),
            "--sample-sheet", str(sheet),
            "--bcl-num-parallel-tiles", str(self.threads),
            "--force",
        ]


class NanoporeDemultiplexer(Demultiplexer):
    """ONT: basecalling/demux with dorado; completion marker is a final_summary file."""

    platform = "ont"

    def __init__(self, *args, kit_name: str = "SQK-NBD114-24", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.kit_name = kit_name

    def is_run_complete(self) -> bool:
        return any(self.run_dir.glob("final_summary_*.txt"))

    def build_command(self) -> list[str]:
        return [
            "dorado", "demux",
            "--kit-name", self.kit_name,
            "--output-dir", str(self.out_dir),
            "--emit-fastq",
            str(self.run_dir),
        ]


def get_demultiplexer(platform: str, *args, **kwargs) -> Demultiplexer:
    registry = {c.platform: c for c in (IlluminaDemultiplexer, NanoporeDemultiplexer)}
    try:
        return registry[platform](*args, **kwargs)
    except KeyError:
        raise DemuxError(f"unsupported platform: {platform}") from None
