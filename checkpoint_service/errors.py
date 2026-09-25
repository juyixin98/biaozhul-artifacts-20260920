"""Exception hierarchy for the checkpoint service."""


class CheckpointError(Exception):
    """Base class for all checkpoint related errors."""


class CheckpointNotFoundError(CheckpointError):
    """No committed checkpoint exists for the run."""


class CheckpointCorruptError(CheckpointError):
    """Checkpoint bytes are torn, tampered with, or undecodable."""


class CheckpointFormatError(CheckpointError):
    """Checkpoint payload is readable but structurally invalid.

    A weights-only payload (parameters without optimizer state, RNG state
    and data cursor) triggers this error on purpose.
    """


class MissingCheckpointFieldError(CheckpointFormatError):
    """A required field is missing from the checkpoint payload."""


class UnsupportedCheckpointVersionError(CheckpointFormatError):
    """Payload format_version is unknown."""


class DatasetMismatchError(CheckpointError):
    """Dataset fingerprint on resume does not match the checkpoint."""


class ServiceError(Exception):
    """Base class for service layer errors."""


class RunNotFoundError(ServiceError):
    """Run id is unknown to the service and absent on disk."""


class RunExistsError(ServiceError):
    """Run id already exists."""


class InvalidRequestError(ServiceError):
    """Caller supplied invalid parameters."""
