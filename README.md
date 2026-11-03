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
| `filemgr.py` | `FileManager`, `AuditLog` | Inventory, md5-verified archiving, guarded delete, batch rename, duplicate finder, JSONL audit log |
| `synth.py` | `generate_fileset` | Deterministic synthetic FASTQ-like file sets for benchmarks (not real data) |
| `bench.py` | `benchmark` | Read-only md5 throughput benchmark with host info |

## Bulk file management

```bash
seqflow synth /data/bench --files 5000 --min-kb 200 --max-kb 800 --paired
seqflow inventory /data/bench --pattern "*.fastq.gz"
seqflow bench /data/bench --workers 1 2 4 8 --repeats 3
seqflow archive /data/run1 /archive/run1 --dry-run --audit-log audit.jsonl
seqflow archive /data/run1 /archive/run1 --remove-source --audit-log audit.jsonl
seqflow prune /data/scratch --older-than-days 30            # dry-run
seqflow prune /data/scratch --older-than-days 30 --execute
seqflow rename /data/run1 --regex "_R(\d)\.fastq\.gz$" --repl "_L001_R\1_001.fastq.gz" --dry-run
```

Safety rules: destructive commands are dry-run by default or need an explicit flag;
the source of an archive is removed only after every copy passes md5 verification;
deletion refuses paths outside the given root and validates the whole list before
removing anything; symlinks are never followed.

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
