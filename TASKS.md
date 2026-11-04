# Roadmap

## Phase 0 - Skeleton (done)
- [x] Package layout, schema, orchestrator, mocked-tool tests, CI

## Phase 1 - Real-data validation (week 1-2)
- [ ] Find small public Illumina test run (BCL) and run `bcl-convert` for real
- [ ] Public ONT pod5 sample + `dorado demux`
- [ ] Integration test marked `@pytest.mark.integration` (skipped without tools)
- [ ] Record file counts, sizes and timings of the real runs in the README

## Phase 2 - Bulk file management (week 2-3)
- [x] Archive / delete / rename with dry-run + audit log
- [x] Synthetic file-set generator and md5 benchmark tooling
- [x] Run the benchmark on large sets and record the results in the README
- [ ] Resume-safe manifests

## Phase 3 - Database depth (week 3-4)
- [x] Alembic migrations
- [x] Parse FastQC/MultiQC output into `qc_metrics`
- [x] Reporting queries (per-project yield, failed runs, QC outliers)
- [x] PostgreSQL integration tests in CI (service container)
- [ ] Test the QC parsers against real FastQC/MultiQC output from the real runs

## Phase 4 - Autonomy + reporting (week 4-5)
- [x] systemd unit / cron example, graceful shutdown, lock file, heartbeat, crash recovery
- [x] Email + Slack channels with retries, daily digest
- [ ] Send one real Slack message and one real e-mail through the configured channels
- [ ] Run the watcher with the real bcl-convert on a real run folder
- [ ] Nextflow RNA-seq/ATAC modules as the downstream pipeline

## Phase 5 - Polish (week 6)
- [ ] PacBio `lima` demultiplexer subclass (optional)
- [ ] Docs, architecture diagram, Zenodo DOI, v1.0 release
