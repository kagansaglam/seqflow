"""Launch downstream pipelines (e.g. Nextflow) once demultiplexing is verified."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .exceptions import PipelineLaunchError


class PipelineLauncher:
    def __init__(self, pipeline: str = "nf-core/fetchngs", profile: str = "docker",
                 work_root: str | Path = "pipeline_runs") -> None:
        self.pipeline = pipeline
        self.profile = profile
        self.work_root = Path(work_root)

    def build_command(self, run_id: str, fastq_dir: str | Path) -> list[str]:
        outdir = self.work_root / run_id
        return ["nextflow", "run", self.pipeline, "-profile", self.profile,
                "--input", str(fastq_dir), "--outdir", str(outdir), "-resume"]

    def _spawn(self, cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)

    def launch(self, run_id: str, fastq_dir: str | Path) -> tuple[list[str], int]:
        cmd = self.build_command(run_id, fastq_dir)
        try:
            proc = self._spawn(cmd)
        except FileNotFoundError as e:
            raise PipelineLaunchError(f"executable not found: {cmd[0]}") from e
        return cmd, proc.returncode
