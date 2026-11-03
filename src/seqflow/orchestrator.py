"""Ties everything together: detect -> demux -> checksum -> record -> launch -> report."""
from __future__ import annotations

import logging
from pathlib import Path

from .checksum import ChecksumManager
from .db import Database
from .demux import get_demultiplexer
from .exceptions import RunIncompleteError, SeqflowError
from .launcher import PipelineLauncher
from .notify import Notifier
from .samplesheet import SampleSheet

log = logging.getLogger("seqflow")


class Orchestrator:
    def __init__(self, db: Database, notifier: Notifier, launcher: PipelineLauncher | None = None,
                 checksums: ChecksumManager | None = None, output_root: str | Path = "fastq") -> None:
        self.db = db
        self.notifier = notifier
        self.launcher = launcher
        self.checksums = checksums or ChecksumManager()
        self.output_root = Path(output_root)

    def process_run(self, run_dir: str | Path, platform: str) -> str:
        run_dir = Path(run_dir)
        run_id = run_dir.name
        self.db.get_or_create_run(run_id, platform, str(run_dir))
        try:
            sheet = SampleSheet.from_file(run_dir / "SampleSheet.csv") if platform == "illumina" else None
            if sheet:
                for r in sheet.records:
                    self.db.add_sample(run_id, r.sample_id, r.project, r.index1, r.index2, r.lane)

            self.db.set_status(run_id, "demultiplexing")
            out_dir = self.output_root / run_id
            demux = get_demultiplexer(platform, run_dir, out_dir, sheet)
            fastqs = demux.run()
            self.db.set_status(run_id, "demultiplexed")

            manifest = self.checksums.write_manifest(out_dir)
            self.checksums.verify_or_raise(manifest)
            self.db.set_status(run_id, "verified")
            self.notifier.notify("info", f"{run_id}: demultiplexed", f"{len(fastqs)} FASTQ files verified")

            if self.launcher:
                self.db.set_status(run_id, "pipeline_running")
                job_id = self.db.add_job(run_id, "downstream", " ".join(self.launcher.build_command(run_id, out_dir)))
                _, code = self.launcher.launch(run_id, out_dir)
                if code != 0:
                    raise SeqflowError(f"downstream pipeline exited {code} (job {job_id})")
            self.db.set_status(run_id, "done")
            return "done"

        except RunIncompleteError as e:
            # Not an error: the run is still being written. Retry on next poll.
            self.db.log_event(run_id, "warning", str(e))
            self.notifier.notify("warning", f"{run_id}: not ready", str(e))
            self.db.set_status(run_id, "detected")
            return "detected"
        except SeqflowError as e:
            self.db.log_event(run_id, "error", str(e))
            self.db.set_status(run_id, "failed")
            self.notifier.notify("error", f"{run_id}: failed", str(e))
            return "failed"
