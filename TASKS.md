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
- [ ] Run the benchmark on a large set on your own machine and record the table in README
- [ ] Resume-safe manifests

## Phase 3 - Database depth (week 3-4)
- [ ] Alembic migrations
- [ ] Parse FastQC/MultiQC output into `qc_metrics`
- [ ] Reporting queries (per-project yield, failed runs, QC outliers)
- [ ] PostgreSQL integration tests in CI (service container)

## Phase 4 - Autonomy + reporting (week 4-5)
- [ ] systemd unit / cron example, graceful shutdown, lock file
- [ ] Email + Slack channels, daily digest
- [ ] Nextflow RNA-seq/ATAC modules as the downstream pipeline

## Phase 5 - Polish (week 6)
- [ ] PacBio `lima` demultiplexer subclass (optional)
- [ ] Docs, architecture diagram, Zenodo DOI, v1.0 release
