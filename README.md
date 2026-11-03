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
| `migrations/` | Alembic | Versioned schema: 0001 initial, 0002 BIGINT file sizes + QC source/uniqueness |
| `qc.py` | `QCIngestor` | Parse FastQC (folders or .zip) and MultiQC general stats into `qc_metrics`, idempotently |
| `reports.py` | `Reporter` | Yield per project, failed runs, QC outliers (median/MAD), event log, run report |

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

## Database, QC and reports

```bash
seqflow db upgrade                                  # create / migrate the schema
seqflow db current
seqflow qc-ingest RUN_ID --fastqc-dir qc/ --multiqc-stats multiqc_data/multiqc_general_stats.txt
seqflow report status
seqflow report yield --since 2026-01-01             # runs, samples, files, bytes per project
seqflow report failed                               # failed runs with their last error
seqflow report qc RUN_ID                            # one row per sample, one column per metric
seqflow report outliers R1.percent_gc               # modified z-score, needs >= 5 values
seqflow report --format json run RUN_ID
```

Schema changes go through Alembic. `tests/test_migrations.py` fails if a model changes
without a matching migration. Every command that touches the database applies pending
migrations first. File sizes are stored as BIGINT: with the original 32-bit column
PostgreSQL rejects any FASTQ above 2 GiB (`integer out of range`).

QC ingestion replaces the previous rows of the same source for a run, so re-running it
never duplicates metrics. FastQC metrics are stored per read (`R1.total_sequences`,
`R2.percent_gc`, ...). Outliers use the modified z-score (median and MAD) because mean
and standard deviation are unreliable with a handful of samples.

## Tests

```bash
pytest                                              # SQLite, PostgreSQL tests are skipped
docker compose up -d db
SEQFLOW_TEST_PG_URL=postgresql+psycopg2://seqflow:seqflow@localhost:5432/seqflow pytest
```

PostgreSQL tests run in a throw-away schema per test. CI runs the full suite against a
PostgreSQL 16 service container on Python 3.10 and 3.12.

## Quick start

```bash
pip install -e ".[dev]"
pytest                                   # SQLite in-memory, no sequencer data needed
docker compose up -d db                  # PostgreSQL
export SEQFLOW_DB=postgresql+psycopg2://seqflow:seqflow@localhost/seqflow
seqflow db upgrade
seqflow watch /data/runs --platform illumina
```

## Status

Early development. See `TASKS.md` for the roadmap. Unit tests use mocked
demultiplexer binaries; validation on real Illumina/ONT runs is still to do.
Benchmarks in this repository are run on synthetic data and say so.
