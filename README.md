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
| `register.py` | `Registrar` | Record existing FASTQ files: md5, size, gzip integrity, read count; no copying |
| `barcodes.py` | `BarcodeSet`, `FastqDemultiplexer` | Demultiplex FASTQ by barcode (separate index read or read header), collision-safe mismatch correction, Undetermined, barcode discovery |
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
* The automated tests use a stand-in `bcl-convert` (`tests/test_end_to_end.py`); the real tool
  was run by hand on one small HiSeq run (see "A real BCL run"), not in CI.

## Registering existing FASTQ files

```bash
seqflow register SRP123456 /data/public/SRP123456 --project public
seqflow report yield
seqflow report qc SRP123456                  # R1.reads_counted / R2.reads_counted per sample
```

Every file is hashed, fully decompressed once (corrupt or truncated gzip and line counts
that are not a multiple of 4 mark the run `failed`) and stored with its read count.
ENA/SRA names (`SRR123_1.fastq.gz`) and Illumina names (`S1_S1_L001_R1_001.fastq.gz`) are
both understood. Running it again updates rows instead of duplicating them.

## Real data run

`register` was run on a public RNA-seq data set: airway smooth muscle cells, BioProject
PRJNA229998 (GEO GSE52778), human, Illumina HiSeq 2000, paired-end 75 bp. Five of the 16
runs were used (SRR1039508 - SRR1039512, untreated and treated cell lines).

| Run | Read pairs (ENA) | Read pairs counted by seqflow |
|---|---:|---:|
| SRR1039508 | 22,935,521 | 22,935,521 (R1) / 22,935,521 (R2) |
| SRR1039509 | 21,155,707 | 21,155,707 / 21,155,707 |
| SRR1039510 | 22,852,619 | 22,852,619 / 22,852,619 |
| SRR1039511 | 21,938,637 | 21,938,637 / 21,938,637 |
| SRR1039512 | 28,136,282 | 28,136,282 / 28,136,282 |

10 files, 15.0 GB, 234,037,532 reads in total, no problems reported. The files were
fetched from the NCBI SRA with `scripts/sra_to_fastq.py` (`prefetch`, `fasterq-dump`,
gzip level 4), because the ENA file server was too slow from the machine used (about
0.1 MB/s against 13 MB/s from the NCBI copy on Amazon). Since the FASTQ files are
regenerated from the `.sra` files, their md5 sums differ from the ones ENA publishes;
the check is the read count against ENA's `read_count` and NCBI's own `.sra` validation.
`scripts/fetch_ena.py` downloads from ENA directly (resumable, md5-verified) and is the
better choice where the ENA server is reachable.

### QC on the same files

FastQC (Ubuntu package, `-t 8`) and MultiQC 1.35 were run on the 10 files under WSL2, then
loaded with `seqflow qc-ingest` (80 FastQC and 60 MultiQC metric values).

* FastQC's `total_sequences` equals the read count seqflow made itself for all 10 files.
* Read length 63 for every read, mean base quality 37.5 - 38.2, GC 49 - 50 %.
* `seqflow report outliers R1.percent_duplicates` flags one sample, SRR1039512
  (56.5 % against a median of 49.9 %, modified z = 11.8). It is also the run with the most
  reads (28.1 M pairs against 21 - 23 M), which usually raises duplication; the flag marks
  a statistical difference, not a failed sample.
* `R1.percent_gc` gives no outliers: FastQC reports it as a whole number, four of five
  samples have 49, so the median absolute deviation is 0 and a score is undefined.

Running this on real output exposed two problems that synthetic test files had hidden, both
fixed with tests: MultiQC names the mates inside the sample name (`SRR1039508_1`) and
reports `total_sequences` in millions, and a metric stored by both FastQC and MultiQC was
counted twice in the outlier statistics.

Not done on this data yet: downstream analysis (alignment, quantification).

## Demultiplexing real data by barcode

```bash
seqflow demux-fastq sequences.fastq.gz --barcode-reads barcodes.fastq.gz \
    --sample-map sample_metadata.tsv --barcode-column barcode-sequence --out demux/ \
    --register-run MOVING_PICTURES
seqflow barcode-scan Undetermined_S0_L001_I1_001.fastq.gz --top 20   # which barcodes are there?
```

The mismatch tolerance is limited by the barcode set: with a minimum pairwise distance d,
at most (d - 1) // 2 errors are corrected, and a read that is equally close to two
barcodes is never assigned. Orientation (barcodes as given or reverse-complemented) is
detected from the first 10,000 reads. Reads and barcode reads are checked to be in step.

Validated on the QIIME 2 "Moving Pictures" data: real Illumina 16S reads from the Earth
Microbiome Project protocol, 12-base Golay barcodes in a separate read, 34 samples.

| | seqflow (1 mismatch) | QIIME 2 `demux emp-single` (published `demux.qzv`, 2024.10) |
|---|---:|---:|
| Reads in | 302,581 | |
| Assigned to the 34 samples | 263,932 (87.2 %) | 263,931 |
| Samples with identical counts | 23 of 34 | |
| Largest difference for a sample | 2 reads (0.05 %) | |

The remaining differences (15 reads in total) come from the error correction: QIIME 2
decodes the Golay barcodes, seqflow corrects by Hamming distance within the barcode set
(minimum distance 5 here). Allowing 2 mismatches assigns 2 more reads. Every input read
ends up in exactly one output file: registering the 35 output files (34 samples and
Undetermined) counts 302,581 reads. The Undetermined reads (12.8 %) are dominated by
well-formed barcodes with 118 - 135 reads each that are not in the sample table, which
looks like samples of the original study that the tutorial subset leaves out; this was not
checked further. 302,581 reads were demultiplexed in about 18 s on the same laptop.

## A real BCL run through `seqflow process`

Run `161020_D00547_0905_BH35KCBCXY`: the reduced HiSeq 2500 run that 10x Genomics publishes
as `cellranger-tiny-bcl-1.2.0` (1 lane, 1 tile, reads 26 + 8 (index) + 98 cycles). BCL
Convert 4.6.2 ran from the nf-core container `quay.io/nf-core/bclconvert:4.6.2` under WSL2
through `deploy/bcl-convert-docker.sh`.

```bash
python3 scripts/make_pf_filters.py RUN_DIR          # see "Missing filter files" below
cp examples/tiny_bcl/SampleSheet.csv RUN_DIR/SampleSheet.csv
seqflow process RUN_DIR --platform illumina --no-launch --output-root /data/fastq
```

| | |
|---|---:|
| Clusters (all passing filter, see below) | 2,136,539 |
| Assigned to the 16 indexes | 2,079,501 (97.3 %) |
| with a perfect index / one mismatch | 2,063,363 / 16,138 |
| Undetermined | 57,038 (2.7 %) |
| seqflow result | status `done`, 17 samples, 34 FASTQ files, 207 MB, md5 manifest verified |

Per 10x library: SI-GA-E3 490,306, SI-GA-F3 523,850, SI-GA-G3 551,082, SI-GA-H3 514,263
reads. The perfect-index counts reported by BCL Convert equal the counts `seqflow
barcode-scan` found in the index reads for every index checked (15 of 16).

Three problems had to be solved on the way:

* **Missing filter files.** The run folder has BCL files but no `*.filter` files. bcl2fastq
  (`--ignore-missing-filter`) treats such tiles as all passing filter; BCL Convert stops
  with "Could not detect filter file format" and, per Illumina, writes no reads for tiles
  without filter data. `scripts/make_pf_filters.py` writes the filter files bcl2fastq
  assumed (layout from the bcl2fastq2 v2.20 guide, cluster count from the BCL header,
  never overwriting existing files).
* **The published sample sheet does not fit the data.** 10x's example sheet names one
  sample with index set SI-P03-C9; its four sequences occur in 7 of 2.1 million reads, so
  nearly everything ended in Undetermined.
* **Finding the real indexes.** `seqflow barcode-scan` on the index reads showed many
  frequent 8-mers rather than four. 10x index sets contain each base once per position;
  three such quadruples were among the 15 most frequent sequences. Looking the sequences up
  in 10x's own table (`tenkit/constants.py` in 10XGenomics/supernova) identified them, and
  a fourth set, as SI-GA-E3, F3, G3 and H3. `examples/tiny_bcl/SampleSheet.csv` lists the 16
  indexes (minimum pairwise distance 4, so BCL Convert's default of one mismatch is safe).

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

Early development. See `TASKS.md` for the roadmap. Run on real public data so far:
BCL Convert on a small HiSeq run through `seqflow process`, barcode demultiplexing of
FASTQ data (checked against QIIME 2), file registration, checksums, FastQC/MultiQC
ingestion and reports. Not yet run on real data: ONT (dorado), PacBio, the watcher with a
live instrument, Slack/e-mail delivery.
Benchmarks in this repository are run on synthetic data and say so.
