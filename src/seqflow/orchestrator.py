"""Ties everything together: detect -> demux -> checksum -> record -> launch -> report."""
from __future__ import annotations

import logging
from pathlib import Path

from .checksum import ChecksumManager
from .db import IN_FLIGHT_STATES, Database
from .demux import get_demultiplexer
from .exceptions import RunIncompleteError, SeqflowError
from .launcher import PipelineLauncher
from .notify import Notifier
from .qc import split_sample_and_read
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
        """Process one run directory; safe to call again for the same run.

        Returns the resulting state: "done", "failed", or "detected" when the
        instrument has not finished writing the run yet.
        """
        run_dir = Path(run_dir)
        run_id = run_dir.name
        self.db.get_or_create_run(run_id, platform, str(run_dir))
        try:
            sheet = SampleSheet.from_file(run_dir / "SampleSheet.csv") if platform == "illumina" else None
            if sheet:
                for r in sheet.records:
                    self.db.add_sample(run_id, r.sample_id, r.project, r.index1, r.index2, r.lane)

            out_dir = self.output_root / run_id
            demux = get_demultiplexer(platform, run_dir, out_dir, sheet)
            if not demux.is_run_complete():
                # Checked before any state change so that polling an unfinished run
                # leaves no trace in the status history.
                raise RunIncompleteError(f"run not complete: {run_dir}")

            self.db.set_status(run_id, "demultiplexing")
            fastqs = demux.run()
            self.db.set_status(run_id, "demultiplexed")

            manifest = self.checksums.write_manifest(out_dir)
            self.checksums.verify_or_raise(manifest)
            self._record_fastqs(run_id, out_dir, manifest)
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
            # Not an error: the run is still being written. Retry on the next poll.
            if self.db.log_event(run_id, "warning", str(e), if_changed=True):
                self.notifier.notify("warning", f"{run_id}: not ready", str(e))
            self.db.set_status(run_id, "detected")
            return "detected"
        except SeqflowError as e:
            return self._fail(run_id, str(e))
        except Exception as e:
            log.exception("unexpected error while processing %s", run_id)
            return self._fail(run_id, f"unexpected {type(e).__name__}: {e}")

    def _fail(self, run_id: str, message: str) -> str:
        self.db.log_event(run_id, "error", message)
        self.db.set_status(run_id, "failed")
        self.notifier.notify("error", f"{run_id}: failed", message)
        return "failed"

    def _record_fastqs(self, run_id: str, out_dir: Path, manifest: Path) -> int:
        """Store every manifest entry in fastq_files, attached to its sample."""
        entries = self.checksums.read_manifest(manifest)
        for rel, md5 in entries.items():
            path = (out_dir / rel).resolve()
            sample, read = split_sample_and_read(path.name)
            self.db.add_fastq(run_id, sample, str(path), read, path.stat().st_size, md5,
                              verified=True)
        return len(entries)

    def recover_interrupted(self) -> list[str]:
        """Reset runs that a crashed process left in an in-flight state, so they are
        picked up again. Only call this while holding the instance lock."""
        stuck = sorted(self.db.run_ids_with_status(IN_FLIGHT_STATES))
        for run_id in stuck:
            self.db.log_event(run_id, "warning",
                              "previous process stopped while this run was in progress; "
                              "it will be processed again")
            self.db.set_status(run_id, "detected")
        if stuck:
            self.notifier.notify("warning", "recovered interrupted runs", ", ".join(stuck))
        return stuck
