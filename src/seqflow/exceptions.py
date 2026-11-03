"""Exception hierarchy. Errors are classified so the notifier can decide
whether something is a warning (continue) or an error (stop the run)."""


class SeqflowError(Exception):
    """Base class for all seqflow errors."""

    severity = "error"


class SampleSheetError(SeqflowError):
    """Sample sheet is missing, malformed or inconsistent."""


class RunIncompleteError(SeqflowError):
    """Run directory has no completion marker yet."""

    severity = "warning"


class DemuxError(SeqflowError):
    """Demultiplexing tool failed or produced unexpected output."""


class ChecksumMismatchError(SeqflowError):
    """A stored md5 does not match the file on disk."""


class QCParseError(SeqflowError):
    """A QC report (FastQC / MultiQC) is missing, empty or not in the expected format."""


class FileOperationError(SeqflowError):
    """A bulk file operation was refused or failed a safety check."""


class PipelineLaunchError(SeqflowError):
    """Downstream pipeline could not be started."""
