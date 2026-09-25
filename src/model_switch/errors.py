"""Exception hierarchy for the model-switch service."""


class ModelSwitchError(Exception):
    """Base class for every error raised by this package."""


class ArtifactNotFoundError(ModelSwitchError):
    """An artifact version (or one of its files) is not on disk."""


class ManifestValidationError(ModelSwitchError):
    """The manifest is missing fields, malformed, or its checksum is wrong."""


class ArtifactIntegrityError(ModelSwitchError):
    """An artifact file failed its SHA-256 / shape / dtype verification."""


class WarmupValidationError(ModelSwitchError):
    """Loading succeeded but the mandatory warm-up produced wrong results."""


class NoActiveModelError(ModelSwitchError):
    """A prediction was requested before any version was activated."""


class SwitchBusyError(ModelSwitchError):
    """Another switch is in progress and the lock wait timed out."""


class ModelDisposedError(ModelSwitchError):
    """A retained model was used after its resources were released."""
