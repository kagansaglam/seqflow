# seqflow

Automated sequencing-run processing in object-oriented Python.

```
run folder appears -> validate SampleSheet -> demultiplex (Illumina / ONT)
   -> md5 manifest + verify -> record in database -> launch Nextflow -> notify
```

## Components

| Module | Class | Role |
|---|---|---|
| `samplesheet.py` | `SampleSheet` | Parse + validate (duplicates, index collisions, bad bases, mixed lengths) |
| `demux.py` | `Demultiplexer` > `IlluminaDemultiplexer`, `NanoporeDemultiplexer` | Platform-specific commands and completion markers |
| `checksum.py` | `ChecksumManager` | Parallel md5, manifests, verification, all-or-nothing bulk rename |
| `db.py` / `models.py` | `Database` | 7-table schema (runs, samples, libraries, fastq_files, qc_metrics, pipeline_jobs, events) |
| `launcher.py` | `PipelineLauncher` | Start downstream Nextflow pipeline |
| `notify.py` | `Notifier` | Log / email; warnings vs errors |
| `orchestrator.py` | `Orchestrator` | State machine tying the steps together |
| `watcher.py` | `RunWatcher` | Poll a directory, retry incomplete runs |

## Quick start

```bash
pip install -e ".[dev]"
pytest                                   # SQLite in-memory, no sequencer data needed
docker compose up -d db                  # PostgreSQL
export SEQFLOW_DB=postgresql+psycopg2://seqflow:seqflow@localhost/seqflow
seqflow watch /data/runs --platform illumina
```

## Status

Early skeleton. See `TASKS.md` for the roadmap. Tests use mocked demultiplexer
binaries; real-data validation is a roadmap item and any benchmark numbers
reported for this project must come from measured runs, not estimates.
