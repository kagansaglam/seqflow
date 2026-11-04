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
| `notify.py` | `Notifier`, `SlackChannel`, `EmailChannel` | Log, Slack webhook, SMTP; errors alert, retries with backoff, credentials only from the environment |
| `orchestrator.py` | `Orchestrator` | State machine tying the steps together |
| `watcher.py` | `RunWatcher` | Poll a directory; error isolation per run, graceful SIGINT/SIGTERM stop, heartbeat file |
| `lock.py` | `LockFile` | Single-instance PID lock, takes over locks of dead processes |
| `retry.py` | `retry_call` | Exponential backoff with jitter |
| `digest.py` | `build_digest` | Daily summary: new runs, yield, failed and stuck runs |
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

## Benchmarks

Synthetic data (random bytes in FASTQ-like file names, `seqflow synth`), not sequencing
data. Host: Windows 11, 8 logical CPUs, Python 3.13, files in the OS page cache after a
warm-up read, so the numbers describe hashing throughput, not disk speed. Measured
2026-11-03.

| Data set | Files | Size | md5 with 8 threads | Throughput |
|---|---:|---:|---:|---:|
| Many small files | 5,000 | 2.49 GB | 1.9 s | 1.3 GB/s |
| Fewer large files | 500 | 2.52 GB | 0.9 s | 2.8 GB/s |

Where the time goes (`md5_probe`-style check on the 500-file set, MB/s with 1 / 8 threads):
md5 of an in-memory buffer 629 / 2059, reading files without hashing 2798 / 4713,
reading and hashing 368 / 2352. Hashing alone scales about 3.3x from 1 to 8 threads on
this CPU.

No speedup factor is reported. Single-thread timings of the same work varied between
200 and 370 MB/s across runs on this machine, so any ratio against that baseline would
mostly measure the baseline's instability. `seqflow bench` therefore interleaves the
worker counts round by round, prints min / median / max, and warns when a speedup
exceeds what the thread count allows or when timings vary by more than 25 %.

## Running unattended

```bash
seqflow watch /data/runs --platform illumina --output-root /data/fastq
seqflow health --heartbeat-file /data/runs/.seqflow-heartbeat.json --max-age 300   # exit 0 = alive
seqflow digest --hours 24                           # summary to Slack / e-mail (--print to just show it)
seqflow requeue RUN_ID                              # let the watcher retry a failed run
```

What the watcher does by itself:

* **One instance only.** A PID lock file (`WATCH_DIR/.seqflow.lock`) stops a second watcher
  from demultiplexing the same run. A lock left by a crashed process is taken over.
* **Crash recovery.** On start, runs left in `demultiplexing`/`demultiplexed`/`verified`/
  `pipeline_running` by a dead process are reset and processed again (with a warning event).
* **Unfinished runs.** A run without its completion marker is re-checked every poll, with one
  warning event and one notification, not one per poll.
* **Failures are isolated and recorded.** A failing run is marked `failed` with its error in
  the event log and an alert; an unexpected exception is treated the same way, so a bug in one
  run does not stop the others. A scan that fails as a whole (for example an unmounted share)
  is reported once and retried, and a "recovered" message follows.
* **Graceful stop.** SIGINT/SIGTERM (Ctrl+C, `systemctl stop`) let the current run finish,
  then the watcher exits and removes its lock. A second signal kills it immediately.
* **Heartbeat.** `WATCH_DIR/.seqflow-heartbeat.json` is rewritten after every scan, so
  `seqflow health` (or cron) can tell a hung watcher from a healthy one.

Alerts and the digest are configured with environment variables only (webhook URL and SMTP
password never appear on the command line or in the database); see
`deploy/seqflow.env.example`. Errors go to Slack and e-mail, warnings only to the log, the
digest to all channels. Sending is retried with backoff for network errors and HTTP 5xx, not
for HTTP 4xx.

`deploy/` has a systemd unit for the watcher (`KillMode=mixed`, long stop timeout), a
service and timer for the daily digest, and a crontab alternative.

Limits worth knowing:

* Ctrl+C in a terminal also reaches the demultiplexer started by the watcher (same process
  group), so that run is marked `failed`; `seqflow requeue` sends it back. The provided
  systemd unit is set up to avoid this (`KillMode=mixed`), but it has not been run under a
  real systemd.
* Two starts at the very same instant that both find a stale lock can still race. This needs
  a crashed holder and simultaneous starts.
* Slack and SMTP delivery is tested against fake servers, not against real services.
* The `bcl-convert` call is tested with a stand-in script (`tests/test_end_to_end.py`), not
  with the real tool.

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
