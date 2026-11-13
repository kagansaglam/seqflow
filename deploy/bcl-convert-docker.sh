#!/bin/sh
# Run BCL Convert from the nf-core container as if it were installed locally, so that
# `seqflow process` / `seqflow watch` can call `bcl-convert`. Install as ~/bin/bcl-convert.
# The container is built by nf-core, not distributed or supported by Illumina.
# Paths must lie under a mounted directory (here /mnt/c, the Windows drive in WSL2).
exec docker run --rm -v /mnt/c:/mnt/c -w /mnt/c quay.io/nf-core/bclconvert:4.6.2 bcl-convert "$@"
